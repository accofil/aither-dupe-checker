from __future__ import annotations

import ast
import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

import requests

from aither_check import (
    AITHER_EVIDENCE_SCHEMA_VERSION,
    AitherError,
    AitherClient,
    AitherPage,
    CheckOutcome,
    DECISION_VERSION,
    IDENTITY_RESOLVER_VERSION,
    MATCHER_VERSION,
    QUERY_POLICY_VERSION,
    ProviderError,
    QbitClient,
    TmdbClient,
    _AITHER_EVIDENCE_FIELDS,
    _AITHER_VALIDATED_EVIDENCE_FIELDS,
    _aither_page_digest,
    _legacy_aither_evidence_loss,
    _legacy_parser_suspect,
    _needs_cached_matcher_recheck,
    _pre_aither_identity_recheck,
    _tmdb_identity_from_variants,
    aither_params,
    candidate_record,
    check_release,
    classify_local_payload,
    evaluate_page,
    enrich_release,
    norm_name,
    parse_release,
    record_terminal,
    scan_completed,
    sanitize_aither_candidate,
    select_tmdb_result,
)
from identity import ContainerMetadataEvidence, TitleVariant
from state_store import StateStore


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="Ok."):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


class CandidateRecordTests(unittest.TestCase):
    def test_terminal_record_preserves_manifest_and_media_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            manifest_evidence = [
                {
                    "fingerprint": "a" * 64,
                    "version": "path-size-v2",
                    "file_count": 2,
                    "payload_size": 30,
                    "main_file_size": 20,
                    "kind": "BDMV",
                }
            ]
            record_terminal(
                store,
                "film",
                "Film.2020.1080p.BluRay.x264-GRP",
                {
                    "disc_manifest_evidence": manifest_evidence,
                    "media_file_sizes": [10, 20, 30],
                    "media_payload_signature": [["mkv", 30]],
                },
                CheckOutcome(status="MANUAL"),
            )

            parsed = store.get_release("film")["parsed"]
            self.assertEqual(
                parsed["media_file_sizes"],
                [10, 20, 30],
            )
            self.assertEqual(parsed["disc_manifest_evidence"], manifest_evidence)
            self.assertEqual(parsed["media_payload_signature"], [["mkv", 30]])

    def test_archive_candidate_contains_transaction_inputs(self):
        torrents = [
            {
                "hash": "b",
                "name": "Film.2020-GRP",
                "state": "stoppedUP",
                "save_path": "/srv",
                "content_path": "/srv/Film.2020-GRP",
                "progress": 1,
            },
            {
                "hash": "a",
                "name": "Film.2020-GRP",
                "state": "uploading",
                "save_path": "/srv",
                "content_path": "/srv/Film.2020-GRP",
                "progress": 1,
            },
        ]
        files = [
            {"name": "Film.2020-GRP/data.rar", "size": 10},
            {"name": "Film.2020-GRP/data.r00", "size": 20},
        ]

        record = candidate_record("key", "Film.2020-GRP", torrents, files)

        self.assertEqual(record["media_kind"], "rar")
        self.assertEqual(record["hashes"], ["a", "b"])
        self.assertEqual(record["running_hashes"], ["a"])
        self.assertEqual(
            record["file_names"],
            ["Film.2020-GRP/data.rar", "Film.2020-GRP/data.r00"],
        )
        self.assertEqual(record["processing"], {"status": "PENDING"})

    def test_plain_candidate_is_immediately_ready(self):
        record = candidate_record(
            "key",
            "Film.2020-GRP",
            [
                {
                    "hash": "a",
                    "name": "Film.2020-GRP",
                    "state": "uploading",
                    "save_path": "/srv",
                    "content_path": "/srv/Film.2020-GRP",
                    "progress": 1,
                }
            ],
            [{"name": "Film.2020-GRP/film.mkv", "size": 10}],
        )
        self.assertEqual(
            record["processing"],
            {
                "status": "READY",
                "ready_path": "/srv/Film.2020-GRP",
                "mode": "plain",
            },
        )

    def test_iso_listing_can_reject_a_game_without_extracting_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "game.iso"
            image.touch()

            def fake_run(command, **kwargs):
                self.assertEqual(command[:4], ["7z", "l", "-slt", "--"])
                kwargs["stdout"].write(
                    b"----------\nPath = setup.exe\nSize = 100\n"
                    b"Path = game/assets/data.bin\nSize = 200\n"
                )
                return mock.Mock(returncode=0)

            with mock.patch("aither_check.subprocess.run", side_effect=fake_run):
                status = classify_local_payload(
                    {"save_path": str(root)},
                    [{"name": "game.iso", "size": 300}],
                )
            self.assertEqual(status, "NOT_MEDIA")


class SearchStrategyTests(unittest.TestCase):
    def setUp(self):
        self.parsed = parse_release("Film.2020.1080p.BluRay.x264-GRP")
        self.torrent = {
            "name": "Film.2020.1080p.BluRay.x264-GRP",
            "save_path": "/missing",
        }

    def test_one_tmdb_request_and_all_candidates_are_examined(self):
        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

            def external_imdb_id(inner, identity):
                raise AssertionError("IMDb fallback must not run")

        calls = []

        class Aither:
            def search_once(inner, params):
                calls.append(params)
                return AitherPage(
                    data=(
                        {
                            "attributes": {
                                "name": "Film.2020.720p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "resolution": "720p",
                                "type": "Encode",
                            }
                        },
                        {
                            "attributes": {
                                "name": "Film.2020.1080p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "resolution": "1080p",
                                "type": "Encode",
                            }
                        },
                    ),
                    has_next=False,
                )

        outcome = check_release(self.parsed, self.torrent, [], Tmdb(), Aither())

        self.assertEqual(outcome.status, "DUPLICATE")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tmdbId"], 123)
        self.assertEqual(calls[0]["perPage"], 100)

    def test_empty_tmdb_page_uses_exactly_one_imdb_fallback(self):
        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

            def external_imdb_id(inner, identity):
                return "tt7654321"

        calls = []

        class Aither:
            def search_once(inner, params):
                calls.append(params)
                return AitherPage(data=(), has_next=False)

        outcome = check_release(self.parsed, self.torrent, [], Tmdb(), Aither())

        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertEqual([call.get("tmdbId") for call in calls], [123, None])
        self.assertEqual(calls[1]["imdbId"], 7654321)

    def test_nonempty_tmdb_page_never_spends_imdb_fallback(self):
        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

            def external_imdb_id(inner, identity):
                raise AssertionError("non-empty TMDb page forbids fallback")

        calls = []

        class Aither:
            def search_once(inner, params):
                calls.append(params)
                return AitherPage(
                    data=(
                        {
                            "attributes": {
                                "name": "Film.2020.720p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "resolution": "720p",
                                "type": "Encode",
                            }
                        },
                    ),
                    has_next=False,
                )

        outcome = check_release(self.parsed, self.torrent, [], Tmdb(), Aither())
        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertEqual(len(calls), 1)

    def test_query_is_broad_and_has_no_technical_or_episode_filters(self):
        params = aither_params(self.parsed, tmdb_id=123)
        self.assertEqual(params["categories[]"], [1])
        self.assertNotIn("types[]", params)
        self.assertNotIn("resolutions[]", params)
        self.assertNotIn("episodeNumber", params)

        episode = parse_release("The.Shards.S01E03.2160p.WEB.H265-GRP")
        episode_params = aither_params(episode, tmdb_id=456)
        self.assertEqual(episode_params["seasonNumber"], 1)
        self.assertNotIn("episodeNumber", episode_params)
        self.assertNotIn("types[]", episode_params)
        self.assertNotIn("resolutions[]", episode_params)

    def test_tmdb_selection_rejects_unrelated_and_wrong_remake(self):
        results = [
            {
                "id": 1,
                "title": "Completely Different",
                "release_date": "2020-01-01",
                "popularity": 9999,
            },
            {
                "id": 2,
                "title": "Film",
                "release_date": "1980-01-01",
                "popularity": 9999,
            },
        ]
        self.assertIsNone(select_tmdb_result("Film", 2020, results, "movie"))
        self.assertIsNone(
            select_tmdb_result(
                "Crash",
                2020,
                [{"id": 3, "title": "Crush", "release_date": "2020-01-01"}],
                "movie",
            )
        )
        self.assertIsNone(
            select_tmdb_result(
                "Film",
                2020,
                [{"id": 4, "title": "Film", "release_date": ""}],
                "movie",
            )
        )
        self.assertIsNone(
            select_tmdb_result(
                "Film",
                2020,
                [
                    {"id": 5, "title": "Film", "release_date": "2020-01-01"},
                    {"id": 6, "title": "Film", "release_date": "2020-02-01"},
                ],
                "movie",
            )
        )

    def test_tmdb_selection_accepts_close_title_with_confirming_year(self):
        result = select_tmdb_result(
            "A Very Long Film Title",
            2020,
            [
                {
                    "id": 7,
                    "title": "Very Long Film Title",
                    "release_date": "2020-01-01",
                }
            ],
            "movie",
        )
        self.assertEqual(result["tmdb_id"], 7)

    def test_tmdb_official_alternative_title_resolves_local_market_name(self):
        class Tmdb(TmdbClient):
            def __init__(inner):
                pass

            def _get_json(inner, url, params):
                if "/search/movie" in url:
                    return {
                        "page": 1,
                        "total_pages": 1,
                        "total_results": 1,
                        "results": [
                            {
                                "id": 5624,
                                "title": "The Corpse Packs His Bags",
                                "original_title": "Der Todesrächer von Soho",
                                "release_date": "1972-10-01",
                            }
                        ]
                    }
                if "/movie/5624/alternative_titles" in url:
                    return {"titles": [{"title": "Death Packs a Suitcase"}]}
                if "/search/tv" in url:
                    return {"results": []}
                raise AssertionError(url)

        result = Tmdb().search(
            {"title": "Death Packs a Suitcase", "year": 1972, "media_type": "movie"}
        )
        self.assertEqual(result["tmdb_id"], 5624)

    def test_unique_same_year_result_without_confirmed_alias_is_rejected(self):
        calls = []

        class Tmdb(TmdbClient):
            def __init__(inner):
                pass

            def _get_json(inner, url, params):
                calls.append(url)
                if "/search/movie" in url:
                    return {
                        "page": 1,
                        "total_pages": 1,
                        "total_results": 1,
                        "results": [
                            {
                                "id": 9,
                                "title": "Unrelated Work",
                                "release_date": "1972-01-01",
                            }
                        ]
                    }
                if "/movie/9/alternative_titles" in url:
                    return {"titles": [{"title": "Still Unrelated"}]}
                if "/search/tv" in url:
                    return {"results": []}
                raise AssertionError(url)

        self.assertIsNone(
            Tmdb().search(
                {"title": "Different Film", "year": 1972, "media_type": "movie"}
            )
        )
        self.assertFalse(any("/search/tv" in url for url in calls))

    def test_tmdb_first_page_is_not_treated_as_globally_unique(self):
        calls = []

        class Tmdb(TmdbClient):
            def __init__(inner):
                pass

            def _get_json(inner, url, params):
                calls.append(url)
                if "/search/movie" not in url:
                    raise AssertionError("incomplete search must not fetch aliases")
                return {
                    "page": 1,
                    "total_pages": 2,
                    "total_results": 21,
                    "results": [
                        {
                            "id": 111,
                            "title": "Crash",
                            "release_date": "1996-01-01",
                        },
                        *[
                            {
                                "id": 200 + index,
                                "title": f"Unrelated {index}",
                                "release_date": "1996-01-01",
                            }
                            for index in range(19)
                        ],
                    ],
                }

        self.assertIsNone(
            Tmdb().search({"title": "Crash", "year": 1996, "media_type": "movie"})
        )
        self.assertEqual(len(calls), 1)

    def test_colon_subtitle_expansion_is_safe_but_plain_prefix_is_not(self):
        class Tmdb(TmdbClient):
            def __init__(inner, remote_title):
                inner.remote_title = remote_title

            def _get_json(inner, url, params):
                if "/search/movie" in url:
                    return {
                        "page": 1,
                        "total_pages": 1,
                        "total_results": 1,
                        "results": [
                            {
                                "id": 49037,
                                "title": inner.remote_title,
                                "release_date": "2004-01-01",
                            }
                        ]
                    }
                if "/movie/49037/alternative_titles" in url:
                    return {"titles": []}
                if "/search/tv" in url:
                    return {"results": []}
                raise AssertionError(url)

        query = {"title": "Hellevator", "year": 2004, "media_type": "movie"}
        self.assertEqual(
            Tmdb("Hellevator: The Bottled Fools").search(query)["tmdb_id"],
            49037,
        )
        self.assertIsNone(Tmdb("Hellevator Pad").search(query))

    def test_missing_ampersand_can_match_one_exact_year_official_title(self):
        class Tmdb(TmdbClient):
            def __init__(inner):
                pass

            def _get_json(inner, url, params):
                if "/search/movie" in url:
                    return {
                        "page": 1,
                        "total_pages": 1,
                        "total_results": 1,
                        "results": [
                            {
                                "id": 533535,
                                "title": "Deadpool & Wolverine",
                                "release_date": "2024-07-24",
                            }
                        ]
                    }
                if "/movie/533535/alternative_titles" in url:
                    return {"titles": []}
                raise AssertionError(url)

        result = Tmdb().search(
            {"title": "Deadpool Wolverine", "year": 2024, "media_type": "movie"}
        )
        self.assertEqual(result["tmdb_id"], 533535)

    def test_one_official_identity_can_collapse_conflicting_market_titles(self):
        class Tmdb(TmdbClient):
            def __init__(inner):
                pass

            def search(inner, parsed):
                if parsed["title"] == "Riptide":
                    return {
                        "tmdb_id": 30523,
                        "media_type": "movie",
                        "title": "The Masque of the Red Death",
                    }
                return {
                    "tmdb_id": 30522,
                    "media_type": "movie",
                    "title": "Masque of the Red Death",
                }

            def alternative_titles(inner, media_type, tmdb_id):
                if tmdb_id == 30523:
                    return ("Riptide", "Masque of the Red Death")
                return ("Edgar Allan Poe's Masque of the Red Death",)

        identity, conflict = _tmdb_identity_from_variants(
            {"media_type": "movie"},
            (
                TitleVariant("Riptide", 1989),
                TitleVariant("Masque of the Red Death", 1989),
            ),
            Tmdb(),
        )
        self.assertFalse(conflict)
        self.assertEqual(identity["tmdb_id"], 30523)

    def test_unique_local_imdb_precedes_noisy_title_search(self):
        name = "Film.2020.tt0052416.1080p.WEB.H264-GRP"
        parsed = parse_release(name)

        class Tmdb:
            def find_by_imdb(inner, imdb_id, media_type):
                self.assertEqual(imdb_id, "tt0052416")
                return {"tmdb_id": 123, "media_type": "movie"}

            def search(inner, query):
                raise AssertionError("trusted local IMDb must precede title search")

        class Aither:
            def search_once(inner, params):
                return AitherPage(
                    data=(
                        {
                            "attributes": {
                                "name": "Film.2020.1080p.WEB-DL.H264-OTHER",
                                "tmdb_id": 123,
                                "type": "WEB-DL",
                                "resolution": "1080p",
                            }
                        },
                    ),
                    has_next=False,
                )

        outcome = check_release(
            parsed,
            {"name": name, "save_path": "/missing"},
            [],
            Tmdb(),
            Aither(),
        )
        self.assertEqual(outcome.status, "DUPLICATE")

    def test_verified_disc_hint_skips_catalog_guessing_and_uses_known_ids(self):
        parsed = parse_release("REAL_LEGEND")
        torrent = {"name": "REAL_LEGEND", "save_path": "/missing"}
        files = [
            {"name": "REAL_LEGEND/BDMV/index.bdmv", "size": 10},
            {"name": "REAL_LEGEND/BDMV/STREAM/00001.m2ts", "size": 100},
        ]

        class Tmdb:
            def find_by_imdb(inner, imdb_id, media_type):
                self.assertEqual((imdb_id, media_type), ("tt1425257", "movie"))
                return {
                    "tmdb_id": 37034,
                    "media_type": "movie",
                    "title": "True Legend",
                }

            def __getattr__(inner, name):
                raise AssertionError("verified IDs must bypass title guessing")

        calls = []

        class Aither:
            def search_once(inner, params, **kwargs):
                calls.append(params)
                return AitherPage(data=(), has_next=False)

        outcome = check_release(
            parsed,
            torrent,
            files,
            Tmdb(),
            Aither(),
            identity_hint={
                "tmdb_id": 37034,
                "imdb_id": "tt1425257",
                "media_type": "movie",
                "title": "True Legend",
                "year": 2010,
            },
        )
        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertEqual(calls[0]["tmdbId"], 37034)
        self.assertEqual(calls[1]["imdbId"], 1425257)

    def test_verified_disc_hint_rejects_mismatched_catalog_pair_before_aither(self):
        class Tmdb:
            def find_by_imdb(inner, imdb_id, media_type):
                return {"tmdb_id": 999, "media_type": "movie"}

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("mismatched override must not reach Aither")

        outcome = check_release(
            parse_release("REAL_LEGEND"),
            {"name": "REAL_LEGEND", "save_path": "/missing"},
            [{"name": "REAL_LEGEND/BDMV/index.bdmv", "size": 10}],
            Tmdb(),
            NeverAither(),
            identity_hint={
                "tmdb_id": 37034,
                "imdb_id": "tt1425257",
                "media_type": "movie",
                "title": "True Legend",
                "year": 2010,
            },
        )
        self.assertEqual(outcome.status, "UNRESOLVED")
        self.assertEqual(outcome.reason, "IDENTITY_OVERRIDE_CATALOG_MISMATCH")

    def test_verified_media_hint_forces_payload_review_before_aither(self):
        class Tmdb:
            def find_by_imdb(inner, imdb_id, media_type):
                self.assertEqual((imdb_id, media_type), ("tt6036728", "tv"))
                return {
                    "tmdb_id": 79012,
                    "media_type": "tv",
                    "title": "The Woman in White",
                }

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("payload review must stop before Aither")

        outcome = check_release(
            parse_release(
                "The.Women.in.White.S01.1080p.DTS-HD.MA.5.1."
                "AVC.REMUX-FraMeSToR"
            ),
            {"name": "The.Women.in.White.S01", "save_path": "/missing"},
            [{"name": "episode.mkv", "size": 100}],
            Tmdb(),
            NeverAither(),
            identity_hint={
                "tmdb_id": 79012,
                "imdb_id": "tt6036728",
                "media_type": "tv",
                "title": "The Woman in White",
                "year": 2018,
                "source": "verified_media_payload_override",
                "payload_review": {
                    "required": True,
                    "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
                },
            },
        )

        self.assertEqual(outcome.status, "MANUAL")
        self.assertEqual(outcome.reason, "OVERLAPPING_EPISODE_PAYLOAD")
        self.assertEqual(outcome.tmdb_id, 79012)
        self.assertEqual(outcome.imdb_id, "tt6036728")

    def test_explicit_double_feature_uses_first_catalog_identity(self):
        name = (
            "Blast-Off Girls (1967) & The Girl, the Body, and the Pill "
            "(1967) 1080p USA Blu-ray AVC DTS-HD MA 2.0"
        )
        parsed = parse_release(name)
        aither_calls = []

        class Tmdb:
            def search(inner, query):
                title = query["title"]
                return {
                    "tmdb_id": 10 if title.startswith("Blast-Off") else 20,
                    "media_type": "movie",
                }

            def external_imdb_id(inner, identity):
                return None

        class Aither:
            def search_once(inner, params):
                aither_calls.append(dict(params))
                return AitherPage(data=(), has_next=False)

        outcome = check_release(
            parsed,
            {"name": name, "save_path": "/missing"},
            [
                {"name": "Release/BDMV/index.bdmv", "size": 100},
                {"name": "Release/BDMV/STREAM/00000.m2ts", "size": 1000},
            ],
            Tmdb(),
            Aither(),
        )
        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertEqual(len(aither_calls), 1)
        self.assertEqual(aither_calls[0]["tmdbId"], 10)


class ProvenOutcomeRegressionTests(unittest.TestCase):
    @staticmethod
    def _disc_files(total=40_000_000_000):
        return [
            {"name": "Release/BDMV/index.bdmv", "size": 100_000},
            {
                "name": "Release/BDMV/STREAM/00000.m2ts",
                "size": total - 100_000,
            },
        ]

    def _disc_outcome(self, local_name, remote_name, *, remote_size=40_000_000_000):
        files = self._disc_files()
        parsed = enrich_release(
            parse_release(local_name),
            local_name,
            {"name": local_name, "total_size": 40_000_000_000},
            files,
        )
        page = AitherPage(
            data=(
                {
                    "attributes": {
                        "name": remote_name,
                        "tmdb_id": 55,
                        "type": "Full Disc",
                        "resolution": "1080p",
                        "size": remote_size,
                        "files": 2,
                        "download_link": "https://aither.cc/secret",
                    }
                },
            ),
            has_next=False,
        )
        return evaluate_page(parsed, page, lookup="tmdb", tmdb_id=55)

    def test_young_and_wild_aus_exact_disc_is_duplicate(self):
        name = "Young.and.Wild.2012.1080p.AUS.Blu-ray.AVC.DTS-HD.MA.5.1"
        outcome = self._disc_outcome(name, name)
        self.assertEqual(outcome.status, "DUPLICATE")
        self.assertEqual(outcome.reason, "MATCHED_TECHNICAL_SIGNATURE")
        serialized = json.dumps(outcome.matched_evidence)
        self.assertNotIn("download_link", serialized)
        self.assertNotIn("secret", serialized)

    def test_the_last_showing_gbr_exact_disc_is_duplicate(self):
        name = "The.Last.Showing.2014.1080p.GBR.Blu-ray.AVC.DTS-HD.MA.5.1"
        self.assertEqual(self._disc_outcome(name, name).status, "DUPLICATE")

    def test_the_island_aus_is_not_same_as_usa_disc(self):
        local = "The.Island.2005.1080p.AUS.Blu-ray.AVC.DTS-HD.MA.5.1"
        remote = "The.Island.2005.1080p.USA.Blu-ray.AVC.DTS-HD.MA.5.1"
        outcome = self._disc_outcome(local, remote)
        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertTrue(outcome.proven_not_found)

    def test_godzilla_cee_is_never_duplicate_of_usa_disc(self):
        local = "Godzilla.1998.1080p.CEE.Blu-ray.AVC.DTS-HD.MA.5.1"
        remote = "Godzilla.1998.1080p.USA.Blu-ray.AVC.DTS-HD.MA.5.1"
        outcome = self._disc_outcome(
            local, remote, remote_size=32_000_000_000
        )
        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertNotEqual(outcome.status, "DUPLICATE")

    def test_conflicting_candidate_totals_are_manual_without_file_list(self):
        name = "Film.2020.1080p.Blu-ray.AVC-GRP"
        parsed = enrich_release(
            parse_release(name),
            name,
            {"name": name, "total_size": 40_000_000_000},
            self._disc_files(),
        )
        missing = object()
        for label, files_value in (("missing", missing), ("numeric count", 2)):
            with self.subTest(files=label):
                attrs = {
                    "name": name,
                    "tmdb_id": 55,
                    "type": "Full Disc",
                    "resolution": "1080p",
                    "size": 40_000_000_000,
                    "total_size": 50_000_000_000,
                }
                if files_value is not missing:
                    attrs["files"] = files_value
                sanitized = sanitize_aither_candidate({"attributes": attrs})
                self.assertEqual(
                    sanitized["attributes"]["total_size"],
                    50_000_000_000,
                )
                outcome = evaluate_page(
                    parsed,
                    AitherPage(data=(sanitized,), has_next=False),
                    lookup="tmdb",
                    tmdb_id=55,
                )
                self.assertEqual(outcome.status, "MANUAL")
                self.assertEqual(outcome.reason, "INVALID_AITHER_CANDIDATE_PAGE")
                self.assertFalse(outcome.proven_not_found)
                self.assertIsNone(outcome.matched_evidence)

    def test_invalid_type_id_cannot_promote_exact_disc_manifest(self):
        name = "Film.2020.1080p.Blu-ray.AVC-GRP"
        disc_files = self._disc_files()
        parsed = enrich_release(
            parse_release(name),
            name,
            {"name": name, "total_size": 40_000_000_000},
            disc_files,
        )

        def outcome_for(type_id):
            candidate = sanitize_aither_candidate(
                {
                    "attributes": {
                        "name": name,
                        "tmdb_id": 55,
                        "type": "Full Disc",
                        "type_id": type_id,
                        "resolution": "1080p",
                        "size": 40_000_000_000,
                        "files": disc_files,
                    }
                }
            )
            return evaluate_page(
                parsed,
                AitherPage(data=(candidate,), has_next=False),
                lookup="tmdb",
                tmdb_id=55,
            )

        for invalid in (999, False, "garbage", -1):
            with self.subTest(type_id=invalid):
                outcome = outcome_for(invalid)
                self.assertEqual(outcome.status, "MANUAL")
                self.assertEqual(outcome.reason, "INVALID_AITHER_CANDIDATE_PAGE")
                self.assertFalse(outcome.proven_not_found)
                self.assertIsNone(outcome.matched_evidence)

        self.assertEqual(outcome_for("1").status, "DUPLICATE")

    def test_sanitizer_preserves_matcher_conflicts_before_exact_manifest(self):
        name = (
            "Film.2020.S01E01.1080p.Blu-ray.AVC."
            "DTS-HD.MA.5.1.10bit-GRP"
        )
        disc_files = self._disc_files()
        parsed = enrich_release(
            parse_release(name),
            name,
            {"name": name, "total_size": 40_000_000_000},
            disc_files,
        )
        conflicts = {
            "codec": {"codec": "H265"},
            "audio": {"audio": "AAC 2.0"},
            "bit depth alias": {"bitDepth": 8},
            "type id alias": {"typeId": 3},
            "season alias": {"seasonNumber": 2},
            "episode alias": {"episodeNumber": 2},
        }
        for label, conflict in conflicts.items():
            with self.subTest(field=label):
                raw = {
                    "attributes": {
                        "name": name,
                        "tmdb_id": 55,
                        "type": "Full Disc",
                        "resolution": "1080p",
                        "size": 40_000_000_000,
                        "files": disc_files,
                        "disc_manifest_kind": "BDMV",
                        **conflict,
                    }
                }
                raw_outcome = evaluate_page(
                    parsed,
                    AitherPage(data=(raw,), has_next=False),
                    lookup="tmdb",
                    tmdb_id=55,
                )
                sanitized = sanitize_aither_candidate(raw)
                for key in (*conflict, "disc_manifest_kind"):
                    self.assertIn(key, sanitized["attributes"])
                sanitized_outcome = evaluate_page(
                    parsed,
                    AitherPage(data=(sanitized,), has_next=False),
                    lookup="tmdb",
                    tmdb_id=55,
                )
                for outcome in (raw_outcome, sanitized_outcome):
                    self.assertEqual(outcome.status, "MANUAL")
                    self.assertFalse(outcome.proven_not_found)
                    self.assertIsNone(outcome.matched_evidence)

    def test_episode_title_disambiguates_tv_and_cache_is_episode_bound(self):
        calls = []

        class Tmdb(TmdbClient):
            def __init__(inner):
                inner._alternative_titles_cache = {}
                inner._episode_identity_cache = {}

            def _get_json(inner, url, params):
                calls.append(url)
                if "/search/tv" in url:
                    return {
                        "page": 1,
                        "total_pages": 1,
                        "total_results": 2,
                        "results": [
                            {"id": 60989, "name": "Married at First Sight"},
                            {"id": 99999, "name": "Married at First Sight"},
                        ]
                    }
                if "/tv/60989/season/20/episode/10" in url:
                    return {
                        "name": "Game On or Game Over",
                        "season_number": 20,
                        "episode_number": 10,
                    }
                if "/tv/99999/season/20/episode/10" in url:
                    raise ProviderError("TMDb HTTP hiba: 404")
                if "/tv/60989/season/20/episode/11" in url:
                    return {
                        "name": "Fighting for Love",
                        "season_number": 20,
                        "episode_number": 11,
                    }
                if "/tv/99999/season/20/episode/11" in url:
                    return {"name": "A Different Episode"}
                raise AssertionError(url)

        client = Tmdb()
        first = parse_release(
            "Married.at.First.Sight.S20E10.Game.On.or.Game.Over."
            "2160p.PCOK.WEB-DL.H265-Kitsune"
        )
        second = parse_release(
            "Married.at.First.Sight.S20E11.Fighting.for.Love."
            "2160p.PCOK.WEB-DL.H265-Kitsune"
        )

        self.assertEqual(client.search(first)["tmdb_id"], 60989)
        self.assertEqual(client.search(first)["tmdb_id"], 60989)
        self.assertEqual(client.search(second)["tmdb_id"], 60989)
        self.assertEqual(sum("/search/tv" in value for value in calls), 1)
        self.assertEqual(sum("/season/20/episode/10" in value for value in calls), 2)

    def test_container_title_is_lazy_exact_fallback_before_aither(self):
        name = (
            "The.Fantasy.of.the.Deer.Warrior.1961.1080p."
            "BluRay.REMUX.AVC-Slope"
        )
        tmdb_queries = []
        aither_queries = []

        class Tmdb:
            def search(inner, query):
                tmdb_queries.append(query["title"])
                if query["title"] == "Da xia mei hua lu":
                    return {
                        "tmdb_id": 278276,
                        "media_type": "movie",
                        "title": "The Fantasy of Deer Warrior",
                    }
                return None

            def external_imdb_id(inner, identity):
                return None

        class Aither:
            def search_once(inner, params, **kwargs):
                aither_queries.append(dict(params))
                return AitherPage(data=(), has_next=False)

        container = ContainerMetadataEvidence(
            imdb_id=None,
            imdb_ids=(),
            tmdb_id=None,
            tmdb_ids=(),
            tmdb_refs=(),
            titles=(
                "Da xia mei hua lu AKA The Fantasy of the Deer Warrior (1961)",
            ),
            series_titles=(),
            probed_files=1,
        )
        with mock.patch(
            "aither_check.scan_matroska_metadata", return_value=container
        ):
            outcome = check_release(
                parse_release(name),
                {"name": name, "save_path": "/missing"},
                [{"name": f"{name}.mkv", "size": 10}],
                Tmdb(),
                Aither(),
            )

        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertIn("Da xia mei hua lu", tmdb_queries)
        self.assertEqual(aither_queries[0]["tmdbId"], 278276)

    def test_conflicting_container_catalog_types_fail_before_aither(self):
        class Tmdb:
            def search(inner, query):
                return None

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("conflicting container IDs must not reach Aither")

        container = ContainerMetadataEvidence(
            imdb_id=None,
            imdb_ids=(),
            tmdb_id=None,
            tmdb_ids=(123,),
            tmdb_refs=(("movie", 123), ("tv", 123)),
            titles=(),
            series_titles=(),
            probed_files=1,
        )
        with mock.patch(
            "aither_check.scan_matroska_metadata", return_value=container
        ):
            outcome = check_release(
                parse_release("Unknown.2020.1080p.WEB-DL.H264-GRP"),
                {"name": "Unknown.2020.1080p.WEB-DL.H264-GRP", "save_path": "/missing"},
                [],
                Tmdb(),
                NeverAither(),
            )

        self.assertEqual(outcome.status, "UNRESOLVED")
        self.assertEqual(outcome.reason, "CONFLICTING_CONTAINER_IDS")

    def test_bare_tv_container_title_is_not_used_as_series_identity(self):
        class Tmdb:
            def search(inner, query):
                if query["title"] == "Finale":
                    return {"tmdb_id": 999, "media_type": "movie", "title": "Finale"}
                return None

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("bare TV TITLE must not reach Aither")

        container = ContainerMetadataEvidence(
            imdb_id=None,
            imdb_ids=(),
            tmdb_id=None,
            tmdb_ids=(),
            tmdb_refs=(),
            titles=("Finale",),
            series_titles=(),
            probed_files=1,
        )
        name = "Unknown.Show.S01E01.1080p.WEB-DL.H264-GRP"
        with mock.patch(
            "aither_check.scan_matroska_metadata", return_value=container
        ):
            outcome = check_release(
                parse_release(name),
                {"name": name, "save_path": "/missing"},
                [],
                Tmdb(),
                NeverAither(),
            )

        self.assertEqual(outcome.status, "UNRESOLVED")

    def test_container_imdb_without_tmdb_mapping_never_chains_title_tmdb(self):
        aither_calls = []

        class Tmdb:
            def search(inner, query):
                if query["title"] == "Other Work":
                    return {"tmdb_id": 999, "media_type": "movie"}
                return None

            def find_by_imdb(inner, imdb_id, media_type):
                return None

        class Aither:
            def search_once(inner, params, **kwargs):
                aither_calls.append(dict(params))
                return AitherPage(data=(), has_next=False)

        container = ContainerMetadataEvidence(
            imdb_id="tt0093258",
            imdb_ids=("tt0093258",),
            tmdb_id=None,
            tmdb_ids=(),
            tmdb_refs=(),
            titles=("Other Work",),
            series_titles=(),
            probed_files=1,
        )
        name = "Unknown.1987.1080p.BluRay.REMUX.AVC-GRP"
        with mock.patch(
            "aither_check.scan_matroska_metadata", return_value=container
        ):
            outcome = check_release(
                parse_release(name),
                {"name": name, "save_path": "/missing"},
                [],
                Tmdb(),
                Aither(),
            )

        self.assertEqual(outcome.status, "NOT_FOUND")
        self.assertEqual(len(aither_calls), 1)
        self.assertNotIn("tmdbId", aither_calls[0])
        self.assertEqual(aither_calls[0]["imdbId"], 93258)

    def test_the_shards_bare_web_vs_hulu_other_group_is_manual(self):
        name = "The.Shards.S01E03.2160p.WEB.H265-CAKES"
        parsed = parse_release(name)
        torrent = {"name": name, "save_path": "/missing"}
        calls = []

        class Tmdb:
            def search(inner, query):
                return {"tmdb_id": 999, "media_type": "tv"}

            def external_imdb_id(inner, identity):
                raise AssertionError("non-empty broad page must finish in one call")

        class Aither:
            def search_once(inner, params):
                calls.append(params)
                return AitherPage(
                    data=(
                        {
                            "attributes": {
                                "name": (
                                    "The.Shards.S01E03.2160p.HULU.WEB-DL."
                                    "DDP5.1.H.265-playWEB"
                                ),
                                "tmdb_id": 999,
                                "type": "WEB-DL",
                                "resolution": "2160p",
                                "files": [
                                    {"name": "The.Shards/episode.mkv", "size": 10}
                                ],
                            }
                        },
                    ),
                    has_next=False,
                )

        outcome = check_release(
            parsed,
            torrent,
            [{"name": "The.Shards/episode.mkv", "size": 10}],
            Tmdb(),
            Aither(),
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertEqual(outcome.reason, "INDETERMINATE_TECHNICAL_SIGNATURE")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["seasonNumber"], 1)
        self.assertNotIn("episodeNumber", calls[0])
        self.assertNotIn("types[]", calls[0])

    def test_indeterminate_page_is_manual_and_never_proven_missing(self):
        name = "Unknown.Disc.2020.1080p.BluRay.AVC"
        parsed = enrich_release(
            parse_release(name), name, {"name": name}, self._disc_files()
        )
        page = AitherPage(
            data=(
                {
                    "attributes": {
                        "name": "Unknown.Disc.2020.1080p.BluRay.AVC",
                        "tmdb_id": 1,
                        "type": "Full Disc",
                        "resolution": "1080p",
                    }
                },
            ),
            has_next=False,
        )
        outcome = evaluate_page(parsed, page, lookup="tmdb", tmdb_id=1)
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_imdb_leading_zeroes_match_numeric_aither_identity(self):
        parsed = parse_release("Film.2020.1080p.WEB.H264-GRP")
        page = AitherPage(
            data=(
                {
                    "attributes": {
                        "name": "Film.2020.1080p.WEB-DL.H264-OTHER",
                        "imdb_id": 52416,
                        "type": "WEB-DL",
                        "resolution": "1080p",
                    }
                },
            ),
            has_next=False,
        )
        outcome = evaluate_page(
            parsed, page, lookup="imdb", imdb_id="tt0052416"
        )
        self.assertEqual(outcome.status, "DUPLICATE")

    def test_live_candidate_page_schema_is_fail_closed(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = enrich_release(
            parse_release(name),
            name,
            {"name": name},
            [{"name": "Film/film.mkv", "size": 10}],
        )

        def candidate(
            *,
            tmdb_id=123,
            files=None,
            total_size=10,
            candidate_name=name,
            category_fields=None,
        ):
            attrs = {
                "name": candidate_name,
                "tmdb_id": tmdb_id,
                "type": "Encode",
                "resolution": "1080p",
                "files": (
                    [{"name": "Film/film.mkv", "size": 10}]
                    if files is None
                    else files
                ),
                "size": total_size,
            }
            attrs.update(category_fields or {})
            return {"attributes": attrs}

        valid = candidate()
        invalid_pages = {
            "wrong identity mixed with valid match": (
                valid,
                candidate(tmdb_id=456),
            ),
            "missing attributes mixed with valid match": (valid, {}),
            "null release name": (candidate(candidate_name=None),),
            "empty release name": (candidate(candidate_name=""),),
            "non-string release name": (candidate(candidate_name=123),),
            "wrong explicit category": (
                candidate(category_fields={"category": "TV", "category_id": 2}),
            ),
            "conflicting category string and id": (
                candidate(
                    category_fields={"category": "MOVIE", "category_id": 2}
                ),
            ),
            "malformed category id": (
                candidate(category_fields={"category_id": "1"}),
            ),
            "resolution mapping without supported shape": (
                candidate(category_fields={"resolution": {}}),
            ),
            "boolean resolution": (
                candidate(category_fields={"resolution": False}),
            ),
            "mapping HDR": (
                candidate(category_fields={"hdr": {}}),
            ),
            "unknown HDR token": (
                candidate(category_fields={"hdr": "GARBAGE"}),
            ),
            "mixed SDR and HDR string": (
                candidate(category_fields={"hdr": "SDR HDR10"}),
            ),
            "mixed SDR and HDR list": (
                candidate(category_fields={"hdr": ["SDR", "HDR10"]}),
            ),
            "unknown codec": (
                candidate(category_fields={"codec": "GARBAGE"}),
            ),
            "conflicting H264 H265 codecs": (
                candidate(category_fields={"codec": "H264 H265"}),
            ),
            "conflicting AV1 H265 codecs": (
                candidate(category_fields={"codec": "AV1 H265"}),
            ),
            "conflicting VC1 H265 codecs": (
                candidate(category_fields={"codec": "VC-1 H265"}),
            ),
            "conflicting MPEG2 H265 codecs": (
                candidate(category_fields={"codec": "MPEG-2 H265"}),
            ),
            "unknown generic audio": (
                candidate(category_fields={"audio": "GARBAGE"}),
            ),
            "unknown audio codec": (
                candidate(category_fields={"audio_codec": "GARBAGE"}),
            ),
            "unknown audio format": (
                candidate(category_fields={"audio_format": "GARBAGE"}),
            ),
            "unknown audio channels": (
                candidate(category_fields={"audio_channels": "9.9"}),
            ),
            "mapping MediaInfo": (
                candidate(category_fields={"media_info": {}}),
            ),
            "list BDInfo": (
                candidate(category_fields={"bd_info": []}),
            ),
            "nested type out-of-range id": (
                candidate(
                    category_fields={
                        "type": {"name": "Encode", "id": 999}
                    }
                ),
            ),
            "nested type boolean id": (
                candidate(
                    category_fields={
                        "type": {"name": "Encode", "id": False}
                    }
                ),
            ),
            "nested type name id conflict": (
                candidate(
                    category_fields={
                        "type": {"name": "WEB-DL", "id": 3}
                    }
                ),
            ),
            "out-of-range resolution id": (
                candidate(category_fields={"resolution_id": 999999}),
            ),
            "resolution name id conflict": (
                candidate(category_fields={"resolution_id": 5}),
            ),
            "malformed nested file": (
                candidate(
                    files=[
                        {"name": "Film/film.mkv", "size": 10},
                        "MALFORMED",
                    ]
                ),
            ),
            "conflicting file name path aliases": (
                candidate(
                    files=[
                        {
                            "name": "Film/film.mkv",
                            "path": "Film/Different.sup",
                            "size": 10,
                        }
                    ]
                ),
            ),
            "traversing secondary file path alias": (
                candidate(
                    files=[
                        {
                            "name": "Film/film.mkv",
                            "path": "../Different.sup",
                            "size": 10,
                        }
                    ]
                ),
            ),
            "empty file name cannot fall back to path": (
                candidate(
                    files=[
                        {
                            "name": "",
                            "path": "Film/film.mkv",
                            "size": 10,
                        }
                    ]
                ),
            ),
            "conflicting file size fields": (
                candidate(
                    files=[
                        {
                            "name": "Film/film.mkv",
                            "size": 10,
                            "length": 999,
                        }
                    ],
                    total_size=999,
                ),
            ),
            "declared total smaller than file sum": (
                candidate(
                    files=[{"name": "Film/film.mkv", "size": 1000}],
                    total_size=1,
                ),
            ),
            "nested manifest count mismatch": (
                candidate(
                    files={
                        "data": [{"name": "Film/film.mkv", "size": 10}],
                        "count": 2,
                    }
                ),
            ),
        }
        for label, data in invalid_pages.items():
            with self.subTest(label=label):
                outcome = evaluate_page(
                    parsed,
                    AitherPage(data=data, has_next=False),
                    lookup="tmdb",
                    tmdb_id=123,
                )
                self.assertEqual(outcome.status, "MANUAL")
                self.assertEqual(outcome.reason, "INVALID_AITHER_CANDIDATE_PAGE")
                self.assertFalse(outcome.proven_not_found)
                self.assertIsNone(outcome.matched_evidence)

        nested_valid = candidate(
            files={
                "data": [{"name": "Film/film.mkv", "size": 10}],
                "count": 1,
                "total": 1,
            }
        )
        outcome = evaluate_page(
            parsed,
            AitherPage(data=(nested_valid,), has_next=False),
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "DUPLICATE")

        structured_valid = candidate(
            files=[
                {
                    "name": "Film/film.mkv",
                    "path": "Film\\film.mkv",
                    "size": 10,
                }
            ],
            category_fields={
                "type": {"name": "Encode", "id": 3},
                "resolution": {"name": "1080p", "id": 3},
                "resolution_id": "3",
                "hdr": "SDR",
                "codec": "H264 AVC",
                "audio": "AAC 2.0",
                "audio_codec": "AAC",
                "audio_format": "AAC",
                "audio_channels": "2.0",
                "media_info": "",
                "bd_info": None,
            }
        )
        outcome = evaluate_page(
            parsed,
            AitherPage(data=(structured_valid,), has_next=False),
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "DUPLICATE")

    def test_same_canonical_codec_aliases_are_valid_controls(self):
        for release_codec, structured_codec in (
            ("H264", "H264 AVC"),
            ("H265", "H265 HEVC"),
        ):
            with self.subTest(codec=structured_codec):
                name = f"Film.2020.1080p.BluRay.{release_codec}-GRP"
                files = [{"name": "Film/film.mkv", "size": 10}]
                parsed = enrich_release(
                    parse_release(name), name, {"name": name}, files
                )
                outcome = evaluate_page(
                    parsed,
                    AitherPage(
                        data=(
                            {
                                "attributes": {
                                    "name": name,
                                    "tmdb_id": 123,
                                    "type": "Encode",
                                    "resolution": "1080p",
                                    "codec": structured_codec,
                                    "files": files,
                                }
                            },
                        ),
                        has_next=False,
                    ),
                    lookup="tmdb",
                    tmdb_id=123,
                )
                self.assertEqual(outcome.status, "DUPLICATE")


class PersistentQueryCacheTests(unittest.TestCase):
    def test_sanitizer_whitelist_covers_all_matching_attrs_keys(self):
        tree = ast.parse(
            (Path(__file__).resolve().parents[1] / "matching.py").read_text(
                encoding="utf-8"
            )
        )

        class AttrKeyVisitor(ast.NodeVisitor):
            def __init__(inner):
                inner.keys = set()
                inner.bindings = {}

            def values(inner, node):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    return {node.value}
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    values = {
                        item.value
                        for item in node.elts
                        if isinstance(item, ast.Constant)
                        and isinstance(item.value, str)
                    }
                    return values if len(values) == len(node.elts) else set()
                if isinstance(node, ast.Name):
                    return set(inner.bindings.get(node.id, ()))
                return set()

            def visit_Call(inner, node):
                if (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "attrs"
                    and node.args
                ):
                    inner.keys.update(inner.values(node.args[0]))
                if (
                    isinstance(node.func, ast.Name)
                    and node.func.id == "_first_attribute"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                    and node.args[0].id == "attrs"
                ):
                    for argument in node.args[1:]:
                        inner.keys.update(inner.values(argument))
                inner.generic_visit(node)

            def visit_Subscript(inner, node):
                if isinstance(node.value, ast.Name) and node.value.id == "attrs":
                    inner.keys.update(inner.values(node.slice))
                inner.generic_visit(node)

            def visit_For(inner, node):
                if isinstance(node.target, ast.Name):
                    previous = inner.bindings.get(node.target.id)
                    inner.bindings[node.target.id] = inner.values(node.iter)
                    for statement in (*node.body, *node.orelse):
                        inner.visit(statement)
                    if previous is None:
                        inner.bindings.pop(node.target.id, None)
                    else:
                        inner.bindings[node.target.id] = previous
                    return
                inner.generic_visit(node)

            def _visit_comprehension(inner, node, outputs):
                previous = {}
                for generator in node.generators:
                    if isinstance(generator.target, ast.Name):
                        key = generator.target.id
                        previous[key] = inner.bindings.get(key)
                        inner.bindings[key] = inner.values(generator.iter)
                    for condition in generator.ifs:
                        inner.visit(condition)
                for output in outputs:
                    inner.visit(output)
                for key, value in previous.items():
                    if value is None:
                        inner.bindings.pop(key, None)
                    else:
                        inner.bindings[key] = value

            def visit_SetComp(inner, node):
                inner._visit_comprehension(node, (node.elt,))

            def visit_ListComp(inner, node):
                inner._visit_comprehension(node, (node.elt,))

            def visit_GeneratorExp(inner, node):
                inner._visit_comprehension(node, (node.elt,))

            def visit_DictComp(inner, node):
                inner._visit_comprehension(node, (node.key, node.value))

        visitor = AttrKeyVisitor()
        visitor.visit(tree)
        self.assertEqual(visitor.keys - _AITHER_EVIDENCE_FIELDS, set())
        self.assertEqual(
            _AITHER_VALIDATED_EVIDENCE_FIELDS,
            _AITHER_EVIDENCE_FIELDS,
        )

    def test_cache_hit_spends_zero_api_and_persists_only_sanitized_page(self):
        class Gate:
            def __init__(inner):
                inner.calls = 0

            def call(inner, request):
                inner.calls += 1
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [
                            {
                                "id": 7,
                                "download_link": "TOP-SECRET-LINK",
                                "attributes": {
                                    "name": "Film.2020.1080p.WEB-DL.H264-GRP",
                                    "tmdb_id": 123,
                                    "type": "WEB-DL",
                                    "category": "TV",
                                    "category_id": 2,
                                    "categoryId": 2,
                                    "resolution": "1080p",
                                    "token": "TOP-SECRET-TOKEN",
                                },
                            }
                        ],
                        "links": {"next": None},
                    }
                )

        class NeverGate:
            def call(inner, request):
                raise AssertionError("persistent cache hit must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            gate = Gate()
            first = AitherClient("https://aither.cc", "secret", gate, store=store)
            first.session = Session()
            params = {"perPage": 100, "tmdbId": 123, "categories[]": [1]}
            callback = mock.Mock()
            live_page = first.search_once(params, before_request=callback)
            self.assertFalse(live_page.from_cache)
            self.assertEqual(gate.calls, 1)
            callback.assert_called_once()
            attempt_nonce = callback.call_args.args[0]
            self.assertEqual(len(attempt_nonce), 64)
            self.assertEqual(set(attempt_nonce) - set("0123456789abcdef"), set())
            self.assertEqual(live_page.attempt_nonce, attempt_nonce)
            self.assertEqual(
                store.get_query(first._cache_key(params))["attempt_nonce"],
                attempt_nonce,
            )
            memory_callback = mock.Mock()
            memory_page = first.search_once(
                params, before_request=memory_callback
            )
            self.assertTrue(memory_page.from_cache)
            self.assertEqual(gate.calls, 1)
            memory_callback.assert_not_called()

            second = AitherClient(
                "https://aither.cc", "different-secret", NeverGate(), store=store
            )
            cached_page = second.search_once(
                params,
                before_request=mock.Mock(
                    side_effect=AssertionError("cache hit must not mark HTTP")
                ),
                allow_persistent_cache=True,
            )
            self.assertTrue(cached_page.from_cache)
            for page in (live_page, memory_page, cached_page):
                self.assertEqual(
                    page.evidence_schema_version,
                    AITHER_EVIDENCE_SCHEMA_VERSION,
                )
                attrs = page.data[0]["attributes"]
                self.assertEqual(attrs["category"], "TV")
                self.assertEqual(attrs["category_id"], 2)
                self.assertEqual(attrs["categoryId"], 2)
                outcome = evaluate_page(
                    parse_release("Film.2020.1080p.WEB.H264-GRP"),
                    page,
                    lookup="tmdb",
                    tmdb_id=123,
                )
                self.assertEqual(outcome.status, "MANUAL")
                self.assertEqual(
                    outcome.reason,
                    "INVALID_AITHER_CANDIDATE_PAGE",
                )
            snapshot = json.dumps(store.snapshot())
            self.assertIn(
                f'"evidence_schema_version": {AITHER_EVIDENCE_SCHEMA_VERSION}',
                snapshot,
            )
            self.assertNotIn("TOP-SECRET", snapshot)
            self.assertNotIn("download_link", snapshot)
            self.assertNotIn("token", snapshot.casefold())

    def test_malformed_candidate_is_rejected_instead_of_becoming_empty(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={"data": ["not-an-object"], "links": {"next": None}}
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        with self.assertRaises(AitherError):
            client.search_once({"perPage": 100, "tmdbId": 123})

    def test_present_corrupt_attributes_cannot_fall_back_to_flat_candidate(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [
                            {
                                "attributes": "CORRUPT",
                                "name": "Film.2020.1080p.WEB-DL.H264-GRP",
                                "tmdb_id": 123,
                                "type": "WEB-DL",
                                "resolution": "1080p",
                            }
                        ],
                        "links": {"next": None},
                    }
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        with self.assertRaises(AitherError):
            client.search_once({"perPage": 100, "tmdbId": 123})

    def test_contradictory_links_and_meta_can_never_prove_not_found(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "last_page": 2,
                            "total": 101,
                            "per_page": 100,
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            client = AitherClient(
                "https://aither.cc", "secret", Gate(), store=store
            )
            client.session = Session()
            params = {"perPage": 100, "tmdbId": 123}
            page = client.search_once(params)
            self.assertFalse(page.complete)
            self.assertFalse(page.has_next)
            outcome = evaluate_page(
                parse_release("Film.2020.1080p.WEB.H264-GRP"),
                page,
                lookup="tmdb",
                tmdb_id=123,
            )
            self.assertEqual(outcome.status, "MANUAL")
            self.assertFalse(outcome.proven_not_found)
            cached = store.get_query(client._cache_key(params))
            self.assertIsNotNone(cached)
            self.assertFalse(cached["complete"])
            self.assertEqual(cached["evidence_digest"], page.evidence_digest)

    def test_unrequested_second_page_metadata_is_rejected(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [{}],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 2,
                            "last_page": 2,
                            "total": 101,
                            "per_page": 100,
                        },
                    }
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        page = client.search_once({"perPage": 100, "tmdbId": 123})
        self.assertFalse(page.complete)
        outcome = evaluate_page(
            parse_release("Film.2020.1080p.WEB.H264-GRP"),
            page,
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_impossible_meta_cardinality_is_persisted_only_as_incomplete(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "last_page": 1,
                            "total": 1,
                            "per_page": 100,
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            client = AitherClient(
                "https://aither.cc", "secret", Gate(), store=store
            )
            client.session = Session()
            params = {"perPage": 100, "tmdbId": 123}
            page = client.search_once(params)
            self.assertFalse(page.complete)
            cached = store.get_query(client._cache_key(params))
            self.assertIsNotNone(cached)
            self.assertFalse(cached["complete"])
            self.assertEqual(cached["candidate_count"], 0)
            self.assertEqual(cached["evidence_digest"], page.evidence_digest)
            outcome = evaluate_page(
                parse_release("Film.2020.1080p.WEB.H264-GRP"),
                page,
                lookup="tmdb",
                tmdb_id=123,
            )
            self.assertEqual(outcome.status, "MANUAL")
            self.assertFalse(outcome.proven_not_found)

    def test_unit3d_partial_meta_empty_page_is_complete_and_cached(self):
        class Gate:
            def __init__(inner):
                inner.calls = 0

            def call(inner, request):
                inner.calls += 1
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "per_page": 100,
                            "from": 1,
                            "to": 0,
                        },
                    }
                )

        class NeverGate:
            def call(inner, request):
                raise AssertionError("partial-meta cache hit must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            gate = Gate()
            client = AitherClient(
                "https://aither.cc", "secret", gate, store=store
            )
            client.session = Session()
            params = {"perPage": 100, "tmdbId": 123}
            live = client.search_once(params)
            self.assertEqual(gate.calls, 1)
            self.assertTrue(live.complete)
            self.assertFalse(live.has_next)
            live_outcome = evaluate_page(
                parse_release("Film.2020.1080p.WEB.H264-GRP"),
                live,
                lookup="tmdb",
                tmdb_id=123,
            )
            self.assertEqual(live_outcome.status, "NOT_FOUND")
            self.assertTrue(live_outcome.proven_not_found)

            resumed_client = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            resumed = resumed_client.search_once(
                params, allow_persistent_cache=True
            )
            self.assertTrue(resumed.from_cache)
            self.assertEqual(resumed.evidence_digest, live.evidence_digest)
            resumed_outcome = evaluate_page(
                parse_release("Film.2020.1080p.WEB.H264-GRP"),
                resumed,
                lookup="tmdb",
                tmdb_id=123,
            )
            self.assertEqual(resumed_outcome.status, "NOT_FOUND")
            self.assertTrue(resumed_outcome.proven_not_found)

    def test_unit3d_partial_meta_is_strict_and_next_is_authoritative(self):
        base_meta = {
            "current_page": 1,
            "per_page": 100,
            "from": 1,
            "to": 0,
        }
        invalid_payloads = {
            "not page one": {
                "data": [],
                "links": {"next": None},
                "meta": {**base_meta, "current_page": 2},
            },
            "wrong from": {
                "data": [],
                "links": {"next": None},
                "meta": {**base_meta, "from": 0},
            },
            "wrong to": {
                "data": [],
                "links": {"next": None},
                "meta": {**base_meta, "to": 1},
            },
            "missing authoritative next": {
                "data": [],
                "links": {},
                "meta": base_meta,
            },
            "short page claims next": {
                "data": [],
                "links": {"next": "https://aither.cc/page/2"},
                "meta": base_meta,
            },
            "mixed partial full meta": {
                "data": [],
                "links": {"next": None},
                "meta": {**base_meta, "total": 0},
            },
            "empty next string": {
                "data": [],
                "links": {"next": ""},
                "meta": base_meta,
            },
            "response page size differs from request": {
                "data": [],
                "links": {"next": None},
                "meta": {**base_meta, "per_page": 1},
            },
        }

        class Gate:
            def call(inner, request):
                return request()

        for label, payload in invalid_payloads.items():
            with self.subTest(label=label):
                class Session:
                    trust_env = True

                    def get(inner, url, headers, params, timeout):
                        return _FakeResponse(payload=payload)

                client = AitherClient("https://aither.cc", "secret", Gate())
                client.session = Session()
                page = client.search_once({"perPage": 100, "tmdbId": 123})
                self.assertFalse(page.complete)
                outcome = evaluate_page(
                    parse_release("Film.2020.1080p.WEB.H264-GRP"),
                    page,
                    lookup="tmdb",
                    tmdb_id=123,
                )
                self.assertEqual(outcome.status, "MANUAL")
                self.assertFalse(outcome.proven_not_found)

    def test_unit3d_partial_meta_valid_next_controls_completeness(self):
        candidate = {
            "attributes": {
                "name": "Film.2020.720p.WEB.H264-OTHER",
                "tmdb_id": 123,
                "type": "WEB-DL",
                "resolution": "720p",
                "codec": "H264",
            }
        }

        class Gate:
            def call(inner, request):
                return request()

        for label, next_link, expected_complete, expected_status in (
            ("last page", None, True, "NOT_FOUND"),
            (
                "more pages",
                "https://aither.cc/api/torrents/filter?page=2",
                False,
                "MANUAL",
            ),
        ):
            with self.subTest(label=label):
                page_data = [candidate] * (100 if next_link else 1)

                class Session:
                    trust_env = True

                    def get(inner, url, headers, params, timeout):
                        return _FakeResponse(
                            payload={
                                "data": page_data,
                                "links": {"next": next_link},
                                "meta": {
                                    "current_page": 1,
                                    "per_page": 100,
                                    "from": 1,
                                    "to": len(page_data),
                                },
                            }
                        )

                client = AitherClient("https://aither.cc", "secret", Gate())
                client.session = Session()
                page = client.search_once({"perPage": 100, "tmdbId": 123})
                self.assertEqual(page.complete, expected_complete)
                self.assertEqual(page.has_next, not expected_complete)
                outcome = evaluate_page(
                    parse_release("Film.2020.1080p.WEB.H264-GRP"),
                    page,
                    lookup="tmdb",
                    tmdb_id=123,
                )
                self.assertEqual(outcome.status, expected_status)
                self.assertEqual(
                    outcome.proven_not_found, expected_status == "NOT_FOUND"
                )

    def test_full_meta_present_range_must_be_consistent(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "last_page": 1,
                            "total": 0,
                            "per_page": 100,
                            "from": 999,
                            "to": 999,
                        },
                    }
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        page = client.search_once({"perPage": 100, "tmdbId": 123})
        self.assertFalse(page.complete)
        outcome = evaluate_page(
            parse_release("Film.2020.1080p.WEB.H264-GRP"),
            page,
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_full_meta_page_size_must_match_request(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "last_page": 1,
                            "total": 0,
                            "per_page": 1,
                        },
                    }
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        page = client.search_once({"perPage": 100, "tmdbId": 123})
        self.assertFalse(page.complete)
        outcome = evaluate_page(
            parse_release("Film.2020.1080p.WEB.H264-GRP"),
            page,
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_invalid_pagination_cached_page_can_only_prove_positive_match(self):
        name = "Film.2020.1080p.WEB.H264-GRP"
        files = [{"name": "Film/film.mkv", "size": 10}]
        parsed = enrich_release(parse_release(name), name, {"name": name}, files)

        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [
                            {
                                "attributes": {
                                    "name": name,
                                    "tmdb_id": 123,
                                    "type": "WEB-DL",
                                    "resolution": "1080p",
                                    "codec": "H264",
                                    "files": files,
                                }
                            }
                        ],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "last_page": 2,
                            "total": 101,
                            "per_page": 100,
                        },
                    }
                )

        class NeverGate:
            def call(inner, request):
                raise AssertionError("incomplete cache resume must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            params = {"perPage": 100, "tmdbId": 123}
            client = AitherClient(
                "https://aither.cc", "secret", Gate(), store=store
            )
            client.session = Session()
            live = client.search_once(params)
            self.assertFalse(live.complete)
            self.assertEqual(
                evaluate_page(
                    parsed, live, lookup="tmdb", tmdb_id=123
                ).status,
                "DUPLICATE",
            )
            cached_record = store.get_query(client._cache_key(params))
            self.assertFalse(cached_record["complete"])
            self.assertEqual(
                cached_record["evidence_digest"], live.evidence_digest
            )

            resumed = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            ).search_once(params, allow_persistent_cache=True)
            self.assertTrue(resumed.from_cache)
            self.assertFalse(resumed.complete)
            self.assertEqual(
                evaluate_page(
                    parsed, resumed, lookup="tmdb", tmdb_id=123
                ).status,
                "DUPLICATE",
            )

    def test_missing_pagination_can_never_prove_not_found(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(payload={"data": []})

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        page = client.search_once({"perPage": 100, "tmdbId": 123})
        self.assertFalse(page.complete)
        outcome = evaluate_page(
            parse_release("Film.2020.1080p.WEB.H264-GRP"),
            page,
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_incomplete_persistent_cache_is_rejected_on_resume(self):
        class NeverGate:
            def call(inner, request):
                raise AssertionError("invalid cache must not reach HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            client = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            params = {"perPage": 100, "tmdbId": 123, "categories[]": [1]}
            store.record_query(
                client._cache_key(params),
                {
                    "query_policy": 2,
                    "data": [],
                    "has_next": False,
                    "candidate_count": 0,
                },
            )
            with self.assertRaises(AitherError):
                client.search_once(params, allow_persistent_cache=True)

    def test_future_release_does_not_reuse_an_old_persistent_empty_page(self):
        class Gate:
            def __init__(inner):
                inner.calls = 0

            def call(inner, request):
                inner.calls += 1
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [
                            {
                                "attributes": {
                                    "name": "New.Release.2026.1080p.WEB-DL.H264-GRP",
                                    "tmdb_id": 123,
                                    "type": "WEB-DL",
                                    "resolution": "1080p",
                                }
                            }
                        ],
                        "links": {"next": None},
                    }
                )

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            params = {"perPage": 100, "tmdbId": 123, "categories[]": [1]}
            seed = AitherClient("https://aither.cc", "secret", Gate(), store=store)
            store.record_query(
                seed._cache_key(params),
                {
                    "query_policy": 2,
                    "data": [],
                    "has_next": False,
                    "complete": True,
                    "candidate_count": 0,
                },
            )

            gate = Gate()
            fresh = AitherClient("https://aither.cc", "secret", gate, store=store)
            fresh.session = Session()
            page = fresh.search_once(params)

            self.assertEqual(gate.calls, 1)
            self.assertEqual(len(page.data), 1)
            self.assertFalse(page.from_cache)

    def test_links_only_page_larger_than_requested_limit_is_never_complete(self):
        class Gate:
            def call(inner, request):
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [
                            {
                                "attributes": {
                                    "name": f"Film.2020.720p.WEB-DL.H264-GRP{i}",
                                    "tmdb_id": 123,
                                    "type": "WEB-DL",
                                    "resolution": "720p",
                                }
                            }
                            for i in range(101)
                        ],
                        "links": {"next": None},
                    }
                )

        client = AitherClient("https://aither.cc", "secret", Gate())
        client.session = Session()
        page = client.search_once({"perPage": 100, "tmdbId": 123})
        self.assertFalse(page.complete)
        outcome = evaluate_page(
            parse_release("Film.2020.1080p.WEB-DL.H264-LOCAL"),
            page,
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertEqual(outcome.status, "MANUAL")
        self.assertFalse(outcome.proven_not_found)

    def test_q3_cache_and_digest_are_bound_to_requested_page_size(self):
        params_100 = {"perPage": 100, "tmdbId": 123, "categories[]": [1]}
        params_50 = {"perPage": 50, "tmdbId": 123, "categories[]": [1]}
        self.assertNotEqual(
            AitherClient._cache_key(params_100),
            AitherClient._cache_key(params_50),
        )
        self.assertNotEqual(
            _aither_page_digest(
                [],
                has_next=False,
                complete=True,
                evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
                requested_per_page=100,
            ),
            _aither_page_digest(
                [],
                has_next=False,
                complete=True,
                evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
                requested_per_page=50,
            ),
        )

    def test_persistent_attempt_resume_never_primes_future_release_memory_cache(self):
        class Gate:
            def __init__(inner):
                inner.calls = 0

            def call(inner, request):
                inner.calls += 1
                return request()

        class Session:
            trust_env = True

            def get(inner, url, headers, params, timeout):
                return _FakeResponse(
                    payload={
                        "data": [],
                        "links": {"next": None},
                        "meta": {
                            "current_page": 1,
                            "per_page": 100,
                            "from": 1,
                            "to": 0,
                        },
                    }
                )

        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(Path(temporary) / "state.json")
            params = {"perPage": 100, "tmdbId": 123, "categories[]": [1]}
            attempt_nonce = "a" * 64
            digest = _aither_page_digest(
                [],
                has_next=False,
                complete=True,
                evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
                requested_per_page=100,
                attempt_nonce=attempt_nonce,
            )
            key = AitherClient._cache_key(params)
            store.record_query(
                key,
                {
                    "query_policy": QUERY_POLICY_VERSION,
                    "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                    "requested_per_page": 100,
                    "attempt_nonce": attempt_nonce,
                    "data": [],
                    "has_next": False,
                    "complete": True,
                    "candidate_count": 0,
                    "evidence_digest": digest,
                },
            )
            gate = Gate()
            client = AitherClient(
                "https://aither.cc", "secret", gate, store=store
            )
            resumed = client.load_persistent_once(
                params, attempt_nonce=attempt_nonce
            )
            self.assertTrue(resumed.from_cache)
            client.session = Session()
            live = client.search_once(params)
            self.assertEqual(gate.calls, 1)
            self.assertFalse(live.from_cache)


class LegacyRepairPolicyTests(unittest.TestCase):
    def test_cached_indeterminate_can_be_replayed_by_a_future_matcher(self):
        record = {
            "status": "MANUAL",
            "terminal": True,
            "decision_version": DECISION_VERSION,
            "query_policy": QUERY_POLICY_VERSION,
            "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
            "matcher_version": MATCHER_VERSION,
            "reason": "CACHED_MATCHER_INDETERMINATE",
            "query_evidence": [
                {
                    "query_policy": QUERY_POLICY_VERSION,
                    "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                    "evidence_digest": "d" * 64,
                    "attempt_nonce": "a" * 64,
                }
            ],
        }

        with mock.patch("aither_check.MATCHER_VERSION", MATCHER_VERSION + 1):
            self.assertTrue(_needs_cached_matcher_recheck(record))

    def test_every_v1_manual_is_quarantined_even_if_it_already_says_full_disc(self):
        self.assertTrue(
            _legacy_parser_suspect(
                {
                    "status": "MANUAL",
                    "parsed": {
                        "release_type": "Full Disc",
                        "medium": "BluRay",
                    },
                }
            )
        )

    def test_unrelated_v1_duplicate_is_not_blanket_reset(self):
        self.assertFalse(
            _legacy_parser_suspect(
                {
                    "status": "DUPLICATE",
                    "parsed": {"release_type": "WEB-DL", "medium": "WEB"},
                }
            )
        )

    def test_v2_no_catalog_identity_gets_one_pre_aither_resolver_retry(self):
        record = {
            "status": "UNRESOLVED",
            "decision_version": 2,
            "reason": "NO_CATALOG_IDENTITY",
        }
        self.assertTrue(_pre_aither_identity_recheck(record))
        self.assertTrue(_legacy_parser_suspect(record))
        record["decision_version"] = 3
        self.assertTrue(_pre_aither_identity_recheck(record))
        self.assertTrue(_legacy_parser_suspect(record))

    def test_current_decision_uses_independent_identity_resolver_version(self):
        record = {
            "status": "UNRESOLVED",
            "decision_version": DECISION_VERSION,
            "reason": "NO_CATALOG_IDENTITY",
            "identity_resolver_version": IDENTITY_RESOLVER_VERSION - 1,
        }
        self.assertTrue(_pre_aither_identity_recheck(record))
        record["identity_resolver_version"] = IDENTITY_RESOLVER_VERSION
        self.assertFalse(_pre_aither_identity_recheck(record))

    def test_v2_identity_retry_requires_proof_that_aither_was_never_called(self):
        base = {
            "status": "UNRESOLVED",
            "decision_version": 2,
            "reason": "NO_CATALOG_IDENTITY",
        }
        for evidence in (
            {"lookup": "tmdb"},
            {"tmdb_id": 123},
            {"imdb_id": "tt0012345"},
            {"query_evidence": [{"lookup": "tmdb"}]},
            {"matched_evidence": {"candidate_id": 1}},
            {"previous_decision": {"status": "LEGACY_SEEN"}},
            {"previous_decision": {"status": "MANUAL"}},
        ):
            with self.subTest(evidence=evidence):
                record = {**base, **evidence}
                self.assertFalse(_pre_aither_identity_recheck(record))
                self.assertFalse(_legacy_parser_suspect(record))

    def test_other_v2_review_outcomes_are_not_blanket_rechecked(self):
        self.assertFalse(
            _legacy_parser_suspect(
                {
                    "status": "UNRESOLVED",
                    "decision_version": 2,
                    "reason": "CONFLICTING_TMDB_IDENTITIES",
                }
            )
        )
        self.assertFalse(
            _legacy_parser_suspect(
                {
                    "status": "NOT_FOUND",
                    "decision_version": 2,
                    "parsed": {"release_type": "Encode", "source": "WEB"},
                }
            )
        )
        self.assertFalse(
            _legacy_parser_suspect(
                {
                    "status": "MANUAL",
                    "decision_version": 2,
                    "reason": "DISC_EVIDENCE_INCOMPLETE",
                }
            )
        )

    def test_lost_legacy_manual_evidence_becomes_manual_without_repeat(self):
        record = {
            "status": "UNRESOLVED",
            "decision_version": 2,
            "reason": "NO_CATALOG_IDENTITY",
            "previous_decision": {
                "status": "MANUAL",
                "lookup": "tmdb",
                "tmdb_id": 30523,
            },
        }
        prior = _legacy_aither_evidence_loss(record)
        self.assertEqual(prior["tmdb_id"], 30523)
        self.assertFalse(_legacy_parser_suspect(record))

        record["previous_decision"]["query_evidence"] = [{"cache_key": "kept"}]
        self.assertIsNone(_legacy_aither_evidence_loss(record))

    def test_unproven_legacy_not_found_evidence_loss_is_detected(self):
        record = {
            "status": "NOT_FOUND",
            "lookup": "imdb",
            "imdb_id": "tt17121280",
            "candidate": {"processing": {"status": "READY"}},
            "processing": {"status": "READY", "ready_path": "/unsafe"},
        }
        prior = _legacy_aither_evidence_loss(record)
        self.assertEqual(prior["imdb_id"], "tt17121280")

        record["proven_not_found"] = True
        self.assertIsNone(_legacy_aither_evidence_loss(record))

        record.pop("proven_not_found")
        record["query_evidence"] = [{"complete": True}]
        self.assertIs(_legacy_aither_evidence_loss(record), record)


class ScanAndClientSafetyTests(unittest.TestCase):
    def test_renamed_same_infohash_is_manual_without_provider_or_aither(self):
        old_name = "Film.2020.1080p.WEB-DL.H264-GRP"
        new_name = "Film.2020.RENAMED.1080p.WEB-DL.H264-GRP"
        torrent_hash = "a" * 40

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": torrent_hash,
                        "name": new_name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, value):
                raise AssertionError("hash alias must stop before qB file/provider work")

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("renamed hash must spend zero provider/Aither API")

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "aither_check.check_release"
        ) as checker:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            old_record = {
                "status": "NOT_FOUND",
                "terminal": True,
                "name": old_name,
                "lookup": "tmdb",
                "tmdb_id": 123,
                "proven_not_found": True,
                "candidate": {"hashes": [torrent_hash]},
                "processing": {
                    "status": "READY",
                    "mode": "plain",
                    "ready_path": "/old/path",
                },
            }
            store.record_release(norm_name(old_name), old_record)
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            renamed = store.get_release(norm_name(new_name))

        checker.assert_not_called()
        self.assertEqual(renamed["status"], "MANUAL")
        self.assertEqual(renamed["reason"], "PREVIOUSLY_CHECKED_INFOHASH")
        self.assertFalse(renamed["proven_not_found"])
        self.assertIsNone(renamed["candidate"])
        self.assertEqual(renamed["processing"], {"status": "NONE"})
        self.assertEqual(renamed["previous_decision"], old_record)
        self.assertEqual(renamed["torrent_hashes"], [torrent_hash])
        self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_terminal_records_bind_valid_hashes_but_ignore_malformed_values(self):
        name = "Fresh.Film.2020.1080p.WEB-DL.H264-GRP"
        torrent_hash = "B" * 40

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": torrent_hash,
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Fresh",
                        "progress": 1,
                    }
                ]

            def files(inner, value):
                return [{"name": "Fresh/film.mkv", "size": 10}]

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "aither_check.check_release",
            return_value=CheckOutcome("MANUAL", reason="TEST_ONLY"),
        ) as checker:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            store.record_release(
                norm_name("Old.Bad.Hash"),
                {
                    "status": "MANUAL",
                    "terminal": True,
                    "candidate": {"hashes": ["not-an-infohash", True, 123]},
                    "processing": {"status": "NONE"},
                },
            )
            scan_completed(Qbit(), object(), object(), store, root / "log")
            record = store.get_release(norm_name(name))

        checker.assert_called_once()
        self.assertEqual(record["torrent_hashes"], [torrent_hash.casefold()])

    def test_foreign_spent_hash_blocks_existing_pre_aither_recheck(self):
        old_name = "Film.2020.1080p.WEB-DL.H264-GRP"
        renamed = "Film.2020.RENAMED.1080p.WEB-DL.H264-GRP"
        torrent_hash = "c" * 40

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": torrent_hash,
                        "name": renamed,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, value):
                raise AssertionError("foreign spent hash must stop before file/provider work")

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("foreign spent hash must spend zero provider/Aither API")

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "aither_check.check_release"
        ) as checker:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            store.record_release(
                norm_name(old_name),
                {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {"hashes": [torrent_hash]},
                    "processing": {"status": "READY", "ready_path": "/old"},
                },
            )
            current = {
                "status": "UNRESOLVED",
                "terminal": True,
                "decision_version": DECISION_VERSION,
                "identity_resolver_version": 1,
                "reason": "NO_CATALOG_IDENTITY",
                "candidate": None,
                "processing": {"status": "NONE"},
            }
            store.record_release(norm_name(renamed), current)
            scan_completed(Qbit(), Never(), Never(), store, root / "log")
            record = store.get_release(norm_name(renamed))

        checker.assert_not_called()
        self.assertEqual(record["status"], "MANUAL")
        self.assertEqual(record["reason"], "PREVIOUSLY_CHECKED_INFOHASH")
        self.assertEqual(record["previous_decision"]["status"], "UNRESOLVED")
        self.assertIsNone(record["candidate"])
        self.assertEqual(record["processing"], {"status": "NONE"})

    def test_hash_backfill_is_append_only_and_old_hash_still_blocks_rename(self):
        old_name = "Film.2020.1080p.WEB-DL.H264-GRP"
        renamed = "Film.2020.RENAMED.1080p.WEB-DL.H264-GRP"
        old_hash = "a" * 40
        replacement_hash = "b" * 40

        class Qbit:
            def __init__(inner, name, torrent_hash):
                inner.name = name
                inner.torrent_hash = torrent_hash

            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": inner.torrent_hash,
                        "name": inner.name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, value):
                raise AssertionError("terminal/hash-alias path must not read files")

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("terminal/hash-alias path must not use provider")

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "aither_check.check_release"
        ) as checker:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            store.record_release(
                norm_name(old_name),
                {
                    "status": "DUPLICATE",
                    "terminal": True,
                    "torrent_hashes": [old_hash],
                    "candidate": None,
                    "processing": {"status": "NONE"},
                },
            )
            scan_completed(
                Qbit(old_name, replacement_hash),
                Never(),
                Never(),
                store,
                root / "log",
            )
            self.assertEqual(
                store.get_release(norm_name(old_name))["torrent_hashes"],
                [old_hash, replacement_hash],
            )
            scan_completed(
                Qbit(renamed, old_hash), Never(), Never(), store, root / "log"
            )
            alias = store.get_release(norm_name(renamed))

        checker.assert_not_called()
        self.assertEqual(alias["status"], "MANUAL")
        self.assertEqual(alias["reason"], "PREVIOUSLY_CHECKED_INFOHASH")

    def test_scan_binds_media_override_to_single_hash_and_records_manual(self):
        name = "The.Women.in.White.S01.1080p.REMUX.AVC-GRP"
        torrent_hash = "a" * 40

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": torrent_hash,
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Woman",
                        "progress": 1,
                    }
                ]

            def files(inner, value):
                self.assertEqual(value, torrent_hash)
                return [{"name": "episode.mkv", "size": 100}]

        class Tmdb:
            def find_by_imdb(inner, imdb_id, media_type):
                return {
                    "tmdb_id": 79012,
                    "media_type": "tv",
                    "title": "The Woman in White",
                }

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("manual override must not call Aither")

        overrides = {
            norm_name(name): {
                "match": {
                    "evidence_type": "media_payload",
                    "media_payload_signature": (("mkv", 100),),
                    "torrent_file_count": 1,
                    "disc_total_size": 100,
                    "torrent_infohash": torrent_hash,
                },
                "identity": {
                    "tmdb_id": 79012,
                    "imdb_id": "tt6036728",
                    "media_type": "tv",
                    "title": "The Woman in White",
                    "year": 2018,
                    "source": "verified_media_payload_override",
                },
                "payload_review": {
                    "required": True,
                    "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
                },
            }
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            summary = scan_completed(
                Qbit(),
                Tmdb(),
                NeverAither(),
                store,
                root / "log",
                identity_overrides=overrides,
            )
            record = store.get_release(norm_name(name))

        self.assertEqual(summary.manual, 1)
        self.assertEqual(record["status"], "MANUAL")
        self.assertEqual(record["reason"], "OVERLAPPING_EPISODE_PAYLOAD")
        self.assertEqual(record["tmdb_id"], 79012)
        self.assertIsNone(record["candidate"])
        self.assertEqual(record["processing"]["status"], "NONE")

    def test_scan_does_not_apply_media_override_to_multi_hash_group(self):
        name = "The.Women.in.White.S01.1080p.REMUX.AVC-GRP"
        first_hash = "a" * 40

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": first_hash,
                        "name": name,
                        "save_path": "/missing",
                        "content_path": "/missing/Woman",
                        "progress": 1,
                    },
                    {
                        "hash": "b" * 40,
                        "name": name,
                        "save_path": "/missing",
                        "content_path": "/missing/Woman",
                        "progress": 1,
                    },
                ]

            def files(inner, value):
                return [{"name": "episode.mkv", "size": 100}]

        overrides = {
            norm_name(name): {
                "match": {
                    "evidence_type": "media_payload",
                    "media_payload_signature": (("mkv", 100),),
                    "torrent_file_count": 1,
                    "disc_total_size": 100,
                    "torrent_infohash": first_hash,
                },
                "identity": {"tmdb_id": 79012},
            }
        }
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "aither_check.check_release",
            return_value=CheckOutcome("MANUAL", reason="TEST_ONLY"),
        ) as checker:
            root = Path(temporary)
            scan_completed(
                Qbit(), object(), object(), StateStore(root / "state.json"),
                root / "log", identity_overrides=overrides,
            )

        self.assertIsNone(checker.call_args.kwargs["identity_hint"])

    @staticmethod
    def _one_release_qbit():
        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": "Film.2020.1080p.BluRay.x264-GRP",
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": "Film/film.mkv", "size": 10}]

        return Qbit()

    @staticmethod
    def _attempt_marker(
        *, lookup="tmdb", tmdb_id=123, imdb_id=None, attempt_nonce="a" * 64
    ):
        return {
            "status": "AITHER_ATTEMPTED",
            "terminal": True,
            "decision_version": DECISION_VERSION,
            "query_policy": QUERY_POLICY_VERSION,
            "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
            "matcher_version": MATCHER_VERSION,
            "reason": "AITHER_HTTP_RESERVED",
            "lookup": lookup,
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "attempt_nonce": attempt_nonce,
            "parsed": {"media_type": "movie", "season": None},
        }

    @staticmethod
    def _store_cached_page(
        store,
        parsed,
        *,
        lookup,
        identity,
        data,
        cache_updates=None,
        include_evidence_schema=True,
        include_evidence_digest=True,
        attempt_nonce="a" * 64,
    ):
        if lookup == "tmdb":
            params = aither_params(parsed, tmdb_id=identity)
            tmdb_id = int(identity)
            imdb_id = None
        else:
            params = aither_params(parsed, imdb_id=identity)
            tmdb_id = None
            imdb_id = identity
        cache_key = AitherClient._cache_key(params)
        cache_record = {
            "query_policy": QUERY_POLICY_VERSION,
            "requested_per_page": int(params["perPage"]),
            "attempt_nonce": attempt_nonce,
            "data": list(data),
            "has_next": False,
            "complete": True,
            "candidate_count": len(data),
        }
        if include_evidence_schema:
            cache_record[
                "evidence_schema_version"
            ] = AITHER_EVIDENCE_SCHEMA_VERSION
        if include_evidence_digest:
            cache_record["evidence_digest"] = _aither_page_digest(
                data,
                has_next=False,
                complete=True,
                evidence_schema_version=(
                    AITHER_EVIDENCE_SCHEMA_VERSION
                    if include_evidence_schema
                    else None
                ),
                requested_per_page=int(params["perPage"]),
                attempt_nonce=attempt_nonce,
            )
        cache_record.update(cache_updates or {})
        store.record_query(cache_key, cache_record)
        evidence = {
            "query_policy": QUERY_POLICY_VERSION,
            "lookup": lookup,
            "tmdb_id": tmdb_id,
            "imdb_id": imdb_id,
            "candidate_count": len(data),
            "has_next": False,
            "complete": True,
            "cache_key": cache_key,
            "from_cache": False,
            "requested_per_page": int(params["perPage"]),
            "attempt_nonce": attempt_nonce,
        }
        if include_evidence_schema:
            evidence[
                "evidence_schema_version"
            ] = AITHER_EVIDENCE_SCHEMA_VERSION
        if include_evidence_digest:
            evidence["evidence_digest"] = cache_record["evidence_digest"]
        return evidence

    @staticmethod
    def _store_old_proven_not_found(store, name, evidence):
        key = norm_name(name)
        store.record_release(
            key,
            {
                "status": "NOT_FOUND",
                "terminal": True,
                "name": name,
                "lookup": evidence[-1]["lookup"],
                "tmdb_id": evidence[-1].get("tmdb_id"),
                "imdb_id": evidence[-1].get("imdb_id"),
                "proven_not_found": True,
                "query_evidence": list(evidence),
                "candidate": {"processing": {"status": "READY"}},
                "processing": {
                    "status": "READY",
                    "mode": "plain",
                    "ready_path": "/old/path",
                },
            },
        )
        return key

    @staticmethod
    def _store_old_indeterminate_manual(store, name, evidence):
        key = norm_name(name)
        store.record_release(
            key,
            {
                "status": "MANUAL",
                "terminal": True,
                "name": name,
                "decision_version": DECISION_VERSION,
                "query_policy": QUERY_POLICY_VERSION,
                "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                "matcher_version": 1,
                "lookup": evidence[-1]["lookup"],
                "tmdb_id": evidence[-1].get("tmdb_id"),
                "imdb_id": evidence[-1].get("imdb_id"),
                "reason": "INDETERMINATE_TECHNICAL_SIGNATURE",
                "query_evidence": list(evidence),
                "candidate": None,
                "processing": {"status": "NONE"},
            },
        )
        return key

    def test_unproven_legacy_not_found_is_quarantined_without_provider_call(self):
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("legacy evidence loss must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name("Film.2020.1080p.BluRay.x264-GRP")
            store.record_release(
                key,
                {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "lookup": "imdb",
                    "imdb_id": "tt17121280",
                    "candidate": {"processing": {"status": "READY"}},
                    "processing": {
                        "status": "READY",
                        "mode": "plain",
                        "ready_path": "/unsafe",
                    },
                },
            )

            prompts = []
            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "nem",
            )

            self.assertEqual(prompts, [])
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "LEGACY_DECISION_NO_REPEAT")
            self.assertEqual(record["imdb_id"], "tt17121280")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(record["previous_decision"]["status"], "NOT_FOUND")
            self.assertEqual(summary.state_counts["MANUAL"], 1)

            second_summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "nem",
            )
            self.assertEqual(prompts, [])
            self.assertEqual(store.get_release(key)["status"], "MANUAL")
            self.assertEqual(second_summary.state_counts["MANUAL"], 1)

    def test_persisted_terminal_false_old_policy_is_quarantined_without_providers(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("terminal false must not reopen the release")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "state.json"
            key = norm_name(name)
            state_path.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "releases": {
                            key: {
                                "status": "NOT_FOUND",
                                "terminal": False,
                                "name": name,
                                "decision_version": 4,
                                "query_policy": 2,
                                "evidence_schema_version": (
                                    AITHER_EVIDENCE_SCHEMA_VERSION
                                ),
                                "matcher_version": MATCHER_VERSION,
                                "proven_not_found": True,
                            }
                        },
                        "rate_limits": {},
                        "queries": {},
                    }
                ),
                encoding="utf-8",
            )
            store = StateStore(state_path)
            summary = scan_completed(
                self._one_release_qbit(),
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            self.assertTrue(store.get_release(key)["terminal"])
            self.assertEqual(store.get_release(key)["status"], "MANUAL")
            self.assertEqual(
                store.get_release(key)["reason"],
                "CACHED_MATCHER_PROVENANCE_INVALID",
            )
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_valid_offline_operator_not_found_survives_q3_cache_bump_only(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        audit_at = "2026-08-11T05:00:00+00:00"

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("offline operator audit must use zero provider/API")

        cases = (
            (1, 0, "NOT_FOUND"),
            (MATCHER_VERSION, 0, "NOT_FOUND"),
            (1, 1, "MANUAL"),
        )
        for matcher_version, network_requests, expected in cases:
            with self.subTest(
                matcher_version=matcher_version,
                network_requests=network_requests,
            ):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    store = StateStore(root / "state.json")
                    key = norm_name(name)
                    store.record_release(
                        key,
                        {
                            "status": "NOT_FOUND",
                            "terminal": True,
                            "name": name,
                            "checked_at": audit_at,
                            "decision_version": DECISION_VERSION,
                            "query_policy": 2,
                            "evidence_schema_version": 2,
                            "matcher_version": matcher_version,
                            "reason": "OFFLINE_OPERATOR_AUDIT_DISTINCT_OR_ABSENT",
                            "proven_not_found": True,
                            "candidate": {"processing": {"status": "READY"}},
                            "processing": {
                                "status": "READY",
                                "mode": "plain",
                                "ready_path": "/missing/Film",
                            },
                            "operator_audit": {
                                "version": 1,
                                "classification": "NOT_FOUND",
                                "basis": "offline exact manifest audit",
                                "audited_at": audit_at,
                                "network_requests": network_requests,
                            },
                            "previous_decision": {"status": "NOT_FOUND"},
                        },
                    )
                    summary = scan_completed(
                        self._one_release_qbit(),
                        Never(),
                        Never(),
                        store,
                        root / "log",
                        imdb=Never(),
                    )
                    record = store.get_release(key)
                    self.assertEqual(record["status"], expected)
                    self.assertEqual(summary.state_counts[expected], 1)

    def test_malformed_operator_audit_numbers_cannot_bypass_q3_quarantine(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        audit_at = "2026-08-11T05:00:00+00:00"

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("malformed audit quarantine must use zero API")

        cases = (
            ("marker bool version", {"version": True}, {}),
            ("marker float version", {"version": 1.0}, {}),
            ("float network count", {"network_requests": 0.0}, {}),
            ("bool matcher version", {}, {"matcher_version": True}),
            ("float matcher version", {}, {"matcher_version": 1.0}),
        )
        for label, marker_updates, record_updates in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                marker = {
                    "version": 1,
                    "classification": "NOT_FOUND",
                    "basis": "offline exact manifest audit",
                    "audited_at": audit_at,
                    "network_requests": 0,
                    **marker_updates,
                }
                record = {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "name": name,
                    "checked_at": audit_at,
                    "decision_version": DECISION_VERSION,
                    "query_policy": 2,
                    "evidence_schema_version": 2,
                    "matcher_version": MATCHER_VERSION,
                    "reason": "OFFLINE_OPERATOR_AUDIT_DISTINCT_OR_ABSENT",
                    "proven_not_found": True,
                    "operator_audit": marker,
                    "previous_decision": {"status": "NOT_FOUND"},
                    **record_updates,
                }
                key = norm_name(name)
                store.record_release(key, record)
                summary = scan_completed(
                    self._one_release_qbit(), Never(), Never(), store, root / "log",
                    imdb=Never(),
                )
                self.assertEqual(store.get_release(key)["status"], "MANUAL")
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_current_matcher_not_found_requires_both_q3_terminal_markers(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("missing provenance must use zero provider/cache")

        for missing in ("query_policy", "evidence_schema_version"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                record = {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "name": name,
                    "decision_version": DECISION_VERSION,
                    "query_policy": QUERY_POLICY_VERSION,
                    "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                    "matcher_version": MATCHER_VERSION,
                    "proven_not_found": True,
                    "candidate": {"processing": {"status": "READY"}},
                    "processing": {"status": "READY"},
                }
                del record[missing]
                key = norm_name(name)
                store.record_release(key, record)
                summary = scan_completed(
                    self._one_release_qbit(), Never(), Never(), store, root / "log",
                    imdb=Never(),
                )
                stored = store.get_release(key)
                self.assertEqual(stored["status"], "MANUAL")
                self.assertEqual(
                    stored["reason"], "CACHED_MATCHER_PROVENANCE_INVALID"
                )
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_cached_multi_query_match_becomes_dupe_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("cached matcher recheck must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            tmdb_evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
            )
            imdb_evidence = self._store_cached_page(
                store,
                parsed,
                lookup="imdb",
                identity="tt0052416",
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.1080p.BluRay.x264-OTHER",
                            "imdb_id": 52416,
                            "type": "Encode",
                            "resolution": "1080p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(
                store, name, [tmdb_evidence, imdb_evidence]
            )

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "DUPLICATE")
            self.assertEqual(record["reason"], "CACHED_MATCHER_MATCH")
            self.assertEqual(record["matcher_version"], MATCHER_VERSION)
            self.assertEqual(len(record["query_evidence"]), 2)
            self.assertTrue(all(item["from_cache"] for item in record["query_evidence"]))
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["DUPLICATE"], 1)

            first_record = record
            with mock.patch(
                "aither_check._reevaluate_not_found_from_cache",
                side_effect=AssertionError("current matcher result must stay terminal"),
            ):
                second = scan_completed(
                    qb,
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
            self.assertEqual(store.get_release(key), first_record)
            self.assertEqual(second.state_counts["DUPLICATE"], 1)

    def test_stale_indeterminate_episode_in_cached_season_pack_becomes_dupe(self):
        name = (
            "Our Sticky Love S01E01 2160p NF WEB-DL DD+ 5.1 Atmos "
            "DV HDR H.265-Kitsune"
        )
        pack_name = (
            "Our Sticky Love S01 2160p NF WEB-DL Dual-Audio DD+ 5.1 "
            "Atmos DV HDR H.265-Kitsune"
        )
        media_size = 8_106_910_298
        parsed = parse_release(name)

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "sticky",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": f"/missing/{name}",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": f"{name}/{name}.mkv", "size": media_size}]

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError(
                    "MANUAL matcher replay must spend zero provider/Aither API"
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": pack_name,
                            "tmdb_id": 123,
                            "type": "WEB-DL",
                            "resolution": "2160p",
                            "files": [
                                {
                                    "name": f"{pack_name}/{name}.mkv",
                                    "size": media_size,
                                },
                                {
                                    "name": (
                                        f"{pack_name}/Our Sticky Love S01E02 "
                                        "2160p NF WEB-DL DD+ 5.1 Atmos DV HDR "
                                        "H.265-Kitsune.mkv"
                                    ),
                                    "size": 8_000_000_000,
                                },
                            ],
                        }
                    }
                ],
            )
            key = self._store_old_indeterminate_manual(store, name, [evidence])

            summary = scan_completed(
                Qbit(), Never(), Never(), store, root / "log", imdb=Never()
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "DUPLICATE")
            self.assertEqual(record["reason"], "CACHED_MATCHER_MATCH")
            self.assertEqual(record["matcher_version"], MATCHER_VERSION)
            self.assertEqual(record["previous_decision"]["status"], "MANUAL")
            self.assertTrue(record["query_evidence"][0]["from_cache"])
            self.assertEqual(summary.state_counts["DUPLICATE"], 1)

            first_record = record
            with mock.patch(
                "aither_check._reevaluate_not_found_from_cache",
                side_effect=AssertionError("current matcher result must stay terminal"),
            ):
                second = scan_completed(
                    Qbit(), Never(), Never(), store, root / "log", imdb=Never()
                )
            self.assertEqual(store.get_release(key), first_record)
            self.assertEqual(second.state_counts["DUPLICATE"], 1)

    def test_stale_indeterminate_manual_invalid_evidence_fails_closed(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError(
                    "invalid MANUAL replay evidence must use zero provider/cache API"
                )

        cases = (
            ("legacy query policy", {"query_policy": 2}),
            ("missing digest", {"evidence_digest": None}),
            ("invalid nonce", {"attempt_nonce": "short"}),
        )
        for label, evidence_updates in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[
                        {
                            "attributes": {
                                "name": "Film.2020.1080p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "type": "Encode",
                                "resolution": "1080p",
                                "files": [{"name": "Film/film.mkv", "size": 10}],
                            }
                        }
                    ],
                )
                evidence.update(evidence_updates)
                key = self._store_old_indeterminate_manual(store, name, [evidence])

                with mock.patch(
                    "aither_check._reevaluate_not_found_from_cache",
                    side_effect=AssertionError("invalid evidence read durable cache"),
                ):
                    summary = scan_completed(
                        self._one_release_qbit(),
                        Never(),
                        Never(),
                        store,
                        root / "log",
                        imdb=Never(),
                    )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(
                    record["reason"], "CACHED_MATCHER_PROVENANCE_INVALID"
                )
                self.assertEqual(record["matcher_version"], MATCHER_VERSION)
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_cached_complete_no_match_refreshes_candidate(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("cached matcher recheck must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.720p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "NOT_FOUND")
            self.assertTrue(record["proven_not_found"])
            self.assertEqual(record["reason"], "CACHED_MATCHER_NO_MATCH")
            self.assertEqual(record["matcher_version"], MATCHER_VERSION)
            self.assertEqual(record["processing"]["ready_path"], "/missing/Film")
            self.assertEqual(record["parsed"]["media_payload_size"], 10)
            self.assertEqual(record["parsed"]["media_main_file_size"], 10)
            self.assertEqual(record["parsed"]["media_file_count"], 1)
            self.assertEqual(summary.state_counts["NOT_FOUND"], 1)

    def test_legacy_nonempty_sanitized_cache_cannot_promote_lost_codec_conflict(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        raw_conflict = {
            "attributes": {
                "name": "Film.2020.1080p.BluRay.x264-OTHER",
                "tmdb_id": 123,
                "type": "Encode",
                "resolution": "1080p",
                "codec": "H265",
                "files": [{"name": "Film/film.mkv", "size": 10}],
            }
        }
        current_outcome = evaluate_page(
            enrich_release(
                parsed,
                name,
                {"name": name},
                [{"name": "Film/film.mkv", "size": 10}],
            ),
            AitherPage(data=(raw_conflict,), has_next=False),
            lookup="tmdb",
            tmdb_id=123,
        )
        self.assertNotEqual(current_outcome.status, "DUPLICATE")

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("legacy cache quarantine must use zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            old_sanitized = {
                "attributes": {
                    key: value
                    for key, value in raw_conflict["attributes"].items()
                    if key != "codec"
                }
            }
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[old_sanitized],
                include_evidence_schema=False,
            )
            key = self._store_old_proven_not_found(store, name, [evidence])
            summary = scan_completed(
                qb, Never(), Never(), store, root / "log", imdb=Never()
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertFalse(record["proven_not_found"])
            self.assertIsNone(record["candidate"])
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_digest_bound_legacy_empty_cache_remains_safe_not_found_evidence(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("legacy empty replay must use zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
                include_evidence_schema=False,
            )
            key = self._store_old_proven_not_found(store, name, [evidence])
            summary = scan_completed(
                self._one_release_qbit(),
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "NOT_FOUND")
            self.assertTrue(record["proven_not_found"])
            self.assertEqual(record["reason"], "CACHED_MATCHER_NO_MATCH")
            self.assertEqual(
                record["evidence_schema_version"],
                AITHER_EVIDENCE_SCHEMA_VERSION,
            )
            self.assertIsNone(
                record["query_evidence"][0]["evidence_schema_version"]
            )
            self.assertEqual(summary.state_counts["NOT_FOUND"], 1)

    def test_missing_page_digest_is_manual_even_for_complete_empty_cache(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("unbound cache must use zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
                include_evidence_digest=False,
            )
            key = self._store_old_proven_not_found(store, name, [evidence])
            summary = scan_completed(
                self._one_release_qbit(),
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertFalse(record["proven_not_found"])
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_replaced_same_key_cache_page_cannot_change_old_decision(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("digest mismatch must use zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            original_evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.720p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(
                store, name, [original_evidence]
            )
            replacement = [
                {
                    "attributes": {
                        "name": "Film.2020.1080p.BluRay.x264-OTHER",
                        "tmdb_id": 123,
                        "type": "Encode",
                        "resolution": "1080p",
                        "files": [{"name": "Film/film.mkv", "size": 10}],
                    }
                }
            ]
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=replacement,
            )
            summary = scan_completed(
                self._one_release_qbit(),
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertIsNone(record["matched_evidence"])
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_malformed_terminal_replay_provenance_is_manual_without_cache_use(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        invalid_markers = {
            "matcher bool": ("matcher_version", False),
            "matcher garbage": ("matcher_version", "garbage"),
            "matcher negative": ("matcher_version", -1),
            "matcher float": ("matcher_version", 0.5),
            "query policy bool": ("query_policy", False),
            "query policy old": ("query_policy", 1),
            "query policy float": ("query_policy", 2.0),
            "schema bool": ("evidence_schema_version", False),
            "schema old": ("evidence_schema_version", 1),
            "schema garbage": ("evidence_schema_version", "garbage"),
            "schema float": ("evidence_schema_version", 2.0),
        }

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("bad provenance must use zero provider API")

        for label, (field, value) in invalid_markers.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[
                        {
                            "attributes": {
                                "name": "Film.2020.1080p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "type": "Encode",
                                "resolution": "1080p",
                                "files": [
                                    {"name": "Film/film.mkv", "size": 10}
                                ],
                            }
                        }
                    ],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])
                previous = store.get_release(key)
                previous[field] = value
                store.record_release(key, previous)
                with mock.patch(
                    "aither_check._reevaluate_not_found_from_cache",
                    side_effect=AssertionError("invalid provenance read cache"),
                ):
                    summary = scan_completed(
                        self._one_release_qbit(),
                        Never(),
                        Never(),
                        store,
                        root / "log",
                        imdb=Never(),
                    )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(
                    record["reason"], "CACHED_MATCHER_PROVENANCE_INVALID"
                )
                self.assertIsNone(record["candidate"])
                self.assertEqual(record["processing"], {"status": "NONE"})
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_invalid_cache_becomes_manual_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid cache must never fall through to providers")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
                cache_updates={"complete": False},
            )
            key = self._store_old_proven_not_found(store, name, [evidence])

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertEqual(record["matcher_version"], MATCHER_VERSION)
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(record["previous_decision"]["status"], "NOT_FOUND")
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_mixed_identity_cache_is_manual_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()
        valid_no_match = {
            "attributes": {
                "name": "Film.2020.720p.BluRay.x264-OTHER",
                "tmdb_id": 123,
                "type": "Encode",
                "resolution": "720p",
                "files": [{"name": "Film/film.mkv", "size": 10}],
            }
        }
        invalid_candidates = {
            "missing attributes": {},
            "different identity": {
                "attributes": {
                    "name": "Other.2020.720p.BluRay.x264-OTHER",
                    "tmdb_id": 456,
                    "type": "Encode",
                    "resolution": "720p",
                    "files": [{"name": "Other/other.mkv", "size": 10}],
                }
            },
        }

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("mixed cache must never call providers")

        for label, invalid_candidate in invalid_candidates.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[valid_no_match, invalid_candidate],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])

                summary = scan_completed(
                    qb,
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
                self.assertEqual(record["matcher_version"], MATCHER_VERSION)
                self.assertIsNone(record["candidate"])
                self.assertEqual(record["processing"], {"status": "NONE"})
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_invalid_page_dominates_other_query_match(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("cached matcher recheck must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            matching_evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.1080p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "1080p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            invalid_evidence = self._store_cached_page(
                store,
                parsed,
                lookup="imdb",
                identity="tt0052416",
                data=[{}],
            )
            key = self._store_old_proven_not_found(
                store,
                name,
                [matching_evidence, invalid_evidence],
            )

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_malformed_nested_files_are_manual_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("malformed cached files must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.720p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [
                                {"name": "Film/film.mkv", "size": 10},
                                "MALFORMED",
                            ],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_missing_candidate_name_is_manual_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid cached name must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": None,
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_wrong_category_is_manual_zero_api(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("wrong cached category must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.1080p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "category": "TV",
                            "category_id": 2,
                            "type": "Encode",
                            "resolution": "1080p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertFalse(record["proven_not_found"])
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_file_totals_are_validated_from_cache(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid cached totals must spend zero API")

        invalid_attrs = {
            "declared total smaller than files": {
                "files": [{"name": "Film/film.mkv", "size": 1000}],
                "size": 1,
            },
            "conflicting item sizes": {
                "files": [
                    {"name": "Film/film.mkv", "size": 10, "length": 999}
                ],
                "size": 999,
            },
            "nested count mismatch": {
                "files": {
                    "data": [{"name": "Film/film.mkv", "size": 10}],
                    "count": 2,
                },
                "size": 10,
            },
        }
        for label, extra_attrs in invalid_attrs.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[
                        {
                            "attributes": {
                                "name": "Film.2020.720p.BluRay.x264-OTHER",
                                "tmdb_id": 123,
                                "type": "Encode",
                                "resolution": "720p",
                                **extra_attrs,
                            }
                        }
                    ],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])
                summary = scan_completed(
                    qb,
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_all_structured_fields_are_schema_validated(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        invalid_fields = {
            "resolution mapping": {"resolution": {}},
            "resolution bool": {"resolution": False},
            "hdr mapping": {"hdr": {}},
            "hdr garbage": {"hdr": "GARBAGE"},
            "mixed SDR HDR string": {"hdr": "SDR HDR10"},
            "mixed SDR HDR list": {"hdr": ["SDR", "HDR10"]},
            "codec garbage": {"codec": "GARBAGE"},
            "codec H264 H265 conflict": {"codec": "H264 H265"},
            "codec AV1 H265 conflict": {"codec": "AV1 H265"},
            "codec VC1 H265 conflict": {"codec": "VC-1 H265"},
            "codec MPEG2 H265 conflict": {"codec": "MPEG-2 H265"},
            "audio garbage": {"audio": "GARBAGE"},
            "audio codec garbage": {"audio_codec": "GARBAGE"},
            "audio format garbage": {"audio_format": "GARBAGE"},
            "audio channels unknown": {"audio_channels": "9.9"},
            "media info mapping": {"media_info": {}},
            "bdinfo list": {"bd_info": []},
            "nested type bad id": {
                "type": {"name": "Encode", "id": 999}
            },
            "nested type bool id": {
                "type": {"name": "Encode", "id": False}
            },
            "nested type name id conflict": {
                "type": {"name": "WEB-DL", "id": 3}
            },
            "resolution id range": {"resolution_id": 999999},
            "resolution name id conflict": {"resolution_id": 5},
        }

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid structured cache must use zero API")

        for label, replacement in invalid_fields.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                attrs = {
                    "name": "Film.2020.1080p.BluRay.x264-GRP",
                    "tmdb_id": 123,
                    "type": "Encode",
                    "resolution": "1080p",
                    "files": [{"name": "Film/film.mkv", "size": 10}],
                }
                attrs.update(replacement)
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[{"attributes": attrs}],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])
                summary = scan_completed(
                    self._one_release_qbit(),
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
                self.assertFalse(record["proven_not_found"])
                self.assertIsNone(record["matched_evidence"])
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_file_path_aliases_are_consistent_in_cache(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        invalid_files = {
            "conflicting aliases": {
                "name": "Film/film.mkv",
                "path": "Film/Different.sup",
                "size": 10,
            },
            "traversing path alias": {
                "name": "Film/film.mkv",
                "path": "../Different.sup",
                "size": 10,
            },
            "empty name alias": {
                "name": "",
                "path": "Film/film.mkv",
                "size": 10,
            },
        }

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid file aliases must use zero API")

        for label, file_item in invalid_files.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=123,
                    data=[
                        {
                            "attributes": {
                                "name": name,
                                "tmdb_id": 123,
                                "type": "Encode",
                                "resolution": "1080p",
                                "files": [file_item],
                            }
                        }
                    ],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])
                summary = scan_completed(
                    self._one_release_qbit(),
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
                self.assertFalse(record["proven_not_found"])
                self.assertIsNone(record["matched_evidence"])
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_full_disc_conflicting_totals_with_file_count_is_manual(self):
        name = "Film.2020.1080p.Blu-ray.AVC-GRP"
        parsed = parse_release(name)
        disc_files = [
            {"name": "Film/BDMV/index.bdmv", "size": 100_000},
            {
                "name": "Film/BDMV/STREAM/00000.m2ts",
                "size": 39_999_900_000,
            },
        ]

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "disc",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                        "total_size": 40_000_000_000,
                    }
                ]

            def files(inner, torrent_hash):
                return disc_files

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid cached totals must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=55,
                data=[
                    {
                        "attributes": {
                            "name": name,
                            "tmdb_id": 55,
                            "type": "Full Disc",
                            "resolution": "1080p",
                            "size": 40_000_000_000,
                            "total_size": 50_000_000_000,
                            "files": 2,
                        }
                    }
                ],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])
            summary = scan_completed(
                Qbit(),
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertFalse(record["proven_not_found"])
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_full_disc_invalid_type_ids_are_manual_zero_api(self):
        name = "Film.2020.1080p.Blu-ray.AVC-GRP"
        parsed = parse_release(name)
        disc_files = [
            {"name": "Film/BDMV/index.bdmv", "size": 100_000},
            {
                "name": "Film/BDMV/STREAM/00000.m2ts",
                "size": 39_999_900_000,
            },
        ]

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "disc",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                        "total_size": 40_000_000_000,
                    }
                ]

            def files(inner, torrent_hash):
                return disc_files

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("invalid cached type_id must spend zero API")

        for invalid in (999, False, "garbage", -1):
            with self.subTest(type_id=invalid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                evidence = self._store_cached_page(
                    store,
                    parsed,
                    lookup="tmdb",
                    identity=55,
                    data=[
                        {
                            "attributes": {
                                "name": name,
                                "tmdb_id": 55,
                                "type": "Full Disc",
                                "type_id": invalid,
                                "resolution": "1080p",
                                "size": 40_000_000_000,
                                "files": disc_files,
                            }
                        }
                    ],
                )
                key = self._store_old_proven_not_found(store, name, [evidence])
                summary = scan_completed(
                    Qbit(),
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    imdb=Never(),
                )
                record = store.get_release(key)
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
                self.assertFalse(record["proven_not_found"])
                self.assertIsNone(record["candidate"])
                self.assertEqual(record["processing"], {"status": "NONE"})
                self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_stale_not_found_terminal_identity_must_match_evidence_tail(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("identity-chain validation must spend zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            evidence = self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
            )
            key = self._store_old_proven_not_found(store, name, [evidence])
            stale = store.get_release(key)
            stale["lookup"] = "imdb"
            stale["tmdb_id"] = None
            stale["imdb_id"] = "tt0052416"
            store.record_release(key, stale)

            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "CACHED_MATCHER_CACHE_INVALID")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_blacklist_is_terminal_before_file_or_provider_calls(self):
        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": "Film.2020.1080p.BluRay.x264-RARBG",
                        "state": "uploading",
                        "save_path": "/srv",
                        "content_path": "/srv/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                raise AssertionError("blacklisted release must not read files")

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("blacklisted release must not use providers")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            self.assertEqual(summary.blacklisted, 1)
            record = next(iter(store.terminal_records().values()))
            self.assertEqual(record["status"], "BLACKLISTED")

    def test_refreshed_blacklist_revokes_existing_ready_before_terminal_skip(self):
        name = "Film.2020.1080p.BluRay.x264-RARBG"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/srv",
                        "content_path": "/srv/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                raise AssertionError("terminal blacklist override must precede files")

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("terminal blacklist override needs no provider")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(
                key,
                {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "decision_version": 2,
                    "candidate": {"content_path": "/wrong"},
                    "processing": {"status": "READY", "ready_path": "/wrong"},
                },
            )
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            record = store.get_release(key)
            self.assertEqual(record["status"], "BLACKLISTED")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["BLACKLISTED"], 1)

    def test_durable_pre_aither_identity_marker_resumes_without_approval(self):
        name = "Film.2020.1080p.WEB.H264-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": "Film/film.mkv", "size": 10}]

        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

            def external_imdb_id(inner, identity):
                return None

        class Aither:
            def search_once(inner, params, **kwargs):
                callback = kwargs.get("before_request")
                if callback is not None:
                    callback("a" * 64)
                return AitherPage(data=(), has_next=False)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(
                key,
                {
                    "status": "RECHECK_REQUIRED",
                    "terminal": True,
                    "decision_version": 3,
                    "reason": "IDENTITY_RESOLVER_PRE_AITHER_RECHECK",
                    "previous_decision": {
                        "status": "UNRESOLVED",
                        "decision_version": 2,
                        "reason": "NO_CATALOG_IDENTITY",
                    },
                },
            )
            summary = scan_completed(
                Qbit(), Tmdb(), Aither(), store, root / "log", repair_input=None
            )
            self.assertEqual(store.get_release(key)["status"], "NOT_FOUND")
            self.assertEqual(summary.state_counts["NOT_FOUND"], 1)

    def test_any_hash_from_aither_makes_whole_group_duplicate_without_api(self):
        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                base = {
                    "name": "Film.2020.1080p.WEB.H264-GRP",
                    "state": "uploading",
                    "save_path": "/missing",
                    "content_path": "/missing/Film",
                    "progress": 1,
                }
                return [
                    {**base, "hash": "primary", "tracker": "https://other.test/a"},
                    {
                        **base,
                        "hash": "second",
                        "tracker": "https://aither.cc/announce/private-value",
                    },
                ]

            def files(inner, torrent_hash):
                return [{"name": "Film/film.mkv", "size": 10}]

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("source_tracker duplicate must not use providers")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            self.assertEqual(summary.duplicate, 1)
            record = next(iter(store.terminal_records().values()))
            self.assertEqual(record["status"], "DUPLICATE")
            self.assertEqual(record["lookup"], "source_tracker")
            self.assertNotIn("private-value", json.dumps(store.snapshot()))

    def test_existing_ready_is_revoked_when_source_becomes_aither(self):
        name = "Film.2020.1080p.WEB.H264-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": name,
                        "tracker": "https://aither.cc/announce/private-value",
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": "Film/film.mkv", "size": 10}]

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("terminal source override needs no provider")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(
                key,
                {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "decision_version": 2,
                    "candidate": {"content_path": "/wrong"},
                    "processing": {
                        "status": "READY",
                        "mode": "plain",
                        "ready_path": "/wrong",
                    },
                },
            )
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            record = store.get_release(key)
            self.assertEqual(record["status"], "DUPLICATE")
            self.assertEqual(record["lookup"], "source_tracker")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["DUPLICATE"], 1)
            self.assertNotIn("private-value", json.dumps(store.snapshot()))

    def test_attempt_marker_resumes_only_from_persistent_cache(self):
        name = "Film.2020.1080p.WEB.H264-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "a",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Film",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": "Film/film.mkv", "size": 10}]

        class NeverProvider:
            def __getattr__(inner, attribute):
                raise AssertionError("crash resume must not rerun a resolver")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("crash resume must never make HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(
                key,
                self._attempt_marker(),
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            params = aither_params(parse_release(name), tmdb_id=123)
            cached_data = [
                {
                    "attributes": {
                        "name": "Film.2020.1080p.WEB-DL.H264-OTHER",
                        "tmdb_id": 123,
                        "type": "WEB-DL",
                        "resolution": "1080p",
                        "files": [{"name": "Film/film.mkv", "size": 10}],
                    }
                }
            ]
            store.record_query(
                aither._cache_key(params),
                {
                    "query_policy": QUERY_POLICY_VERSION,
                    "evidence_schema_version": AITHER_EVIDENCE_SCHEMA_VERSION,
                    "requested_per_page": 100,
                    "attempt_nonce": "a" * 64,
                    "data": cached_data,
                    "has_next": False,
                    "complete": True,
                    "candidate_count": 1,
                    "evidence_digest": _aither_page_digest(
                        cached_data,
                        has_next=False,
                        complete=True,
                        evidence_schema_version=AITHER_EVIDENCE_SCHEMA_VERSION,
                        requested_per_page=100,
                        attempt_nonce="a" * 64,
                    ),
                },
            )
            summary = scan_completed(
                Qbit(), NeverProvider(), aither, store, root / "log",
                imdb=NeverProvider(),
            )
            self.assertEqual(store.get_release(key)["status"], "DUPLICATE")
            self.assertEqual(summary.state_counts["DUPLICATE"], 1)

    def test_attempt_marker_uses_its_identity_not_a_foreign_resolver_cache(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("marker resume must use zero provider/API")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("marker resume must use zero Aither HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(key, self._attempt_marker(tmdb_id=123))
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.720p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=456,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.1080p.BluRay.x264-OTHER",
                            "tmdb_id": 456,
                            "type": "Encode",
                            "resolution": "1080p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            first = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            second = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "NOT_FOUND")
            self.assertEqual(record["tmdb_id"], 123)
            self.assertTrue(record["proven_not_found"])
            self.assertEqual(first.state_counts["NOT_FOUND"], 1)
            self.assertEqual(second.already_terminal, 1)

    def test_attempt_marker_rejects_stale_same_identity_cache_generation(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("stale generation quarantine must use zero API")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("stale generation quarantine must use zero HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(
                key, self._attempt_marker(attempt_nonce="a" * 64)
            )
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                attempt_nonce="b" * 64,
                data=[
                    {
                        "attributes": {
                            "name": "Film.2020.720p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "720p",
                            "files": [{"name": "Film/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            first = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            second = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY")
            self.assertEqual(first.state_counts["MANUAL"], 1)
            self.assertEqual(second.already_terminal, 1)

    def test_attempt_marker_is_resumed_before_any_new_release_provider_work(self):
        new_name = "A.New.Film.2020.1080p.BluRay.x264-GRP"
        marked_name = "Z.Marked.Film.2020.1080p.BluRay.x264-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "new",
                        "name": new_name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/A",
                        "progress": 1,
                    },
                    {
                        "hash": "marked",
                        "name": marked_name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Z",
                        "progress": 1,
                    },
                ]

            def files(inner, torrent_hash):
                prefix = "Z" if torrent_hash == "marked" else "A"
                return [{"name": f"{prefix}/film.mkv", "size": 10}]

        class StopsNewRelease:
            def search(inner, parsed):
                raise ProviderError("stop after marker preflight")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("marker preflight must use zero Aither HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            marked_key = norm_name(marked_name)
            store.record_release(marked_key, self._attempt_marker())
            self._store_cached_page(
                store,
                parse_release(marked_name),
                lookup="tmdb",
                identity=123,
                data=[
                    {
                        "attributes": {
                            "name": "Z.Marked.Film.2020.1080p.BluRay.x264-OTHER",
                            "tmdb_id": 123,
                            "type": "Encode",
                            "resolution": "1080p",
                            "files": [{"name": "Z/film.mkv", "size": 10}],
                        }
                    }
                ],
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            summary = scan_completed(
                Qbit(), StopsNewRelease(), aither, store, root / "log"
            )
            self.assertEqual(summary.errors, 1)
            self.assertEqual(store.get_release(marked_key)["status"], "DUPLICATE")

    def test_attempt_marker_rejects_old_policy_cache_without_any_retry(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("old cache quarantine must use zero provider/API")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("old cache quarantine must use zero HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(key, self._attempt_marker())
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            params = aither_params(parsed, tmdb_id=123)
            data = [
                {
                    "attributes": {
                        "name": "Film.2020.1080p.BluRay.x264-OTHER",
                        "tmdb_id": 123,
                        "type": "Encode",
                        "resolution": "1080p",
                        "files": [{"name": "Film/film.mkv", "size": 10}],
                    }
                }
            ]
            store.record_query(
                aither._cache_key(params),
                {
                    "query_policy": 2,
                    "evidence_schema_version": 2,
                    "requested_per_page": 100,
                    "data": data,
                    "has_next": False,
                    "complete": True,
                    "candidate_count": 1,
                    "evidence_digest": _aither_page_digest(
                        data,
                        has_next=False,
                        complete=True,
                        evidence_schema_version=2,
                        requested_per_page=100,
                    ),
                },
            )
            first = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            second = scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY")
            self.assertEqual(record["previous_decision"]["status"], "AITHER_ATTEMPTED")
            self.assertEqual(first.state_counts["MANUAL"], 1)
            self.assertEqual(second.already_terminal, 1)

    def test_attempt_marker_tmdb_empty_cache_requires_forbidden_fallback(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("TMDb-empty resume must use zero provider/API")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("TMDb-empty resume must use zero HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(key, self._attempt_marker())
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY")

    def test_attempt_marker_missing_digest_is_manual_without_retry(self):
        name = "Film.2020.1080p.BluRay.x264-GRP"
        parsed = parse_release(name)

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("missing digest must use zero provider/API")

        class NeverGate:
            def call(inner, request):
                raise AssertionError("missing digest must use zero HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            store.record_release(key, self._attempt_marker())
            self._store_cached_page(
                store,
                parsed,
                lookup="tmdb",
                identity=123,
                data=[],
                include_evidence_digest=False,
            )
            aither = AitherClient(
                "https://aither.cc", "secret", NeverGate(), store=store
            )
            scan_completed(
                self._one_release_qbit(), Never(), aither, store, root / "log",
                imdb=Never(),
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY")

    def test_manifest_first_not_media_uses_no_catalog_or_aither_provider(self):
        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "game",
                        "name": "An_Abyss_of_Dreams_v1.0.2-DINOByTES",
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/game",
                        "progress": 1,
                    }
                ]

            def files(inner, torrent_hash):
                return [{"name": "setup.exe", "size": 1000}]

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("NOT_MEDIA must not use providers")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            summary = scan_completed(Qbit(), Never(), Never(), store, root / "log")
            self.assertEqual(summary.not_media, 1)
            record = next(iter(store.terminal_records().values()))
            self.assertEqual(record["status"], "NOT_MEDIA")
            self.assertEqual(record["processing"], {"status": "NONE"})

    def test_legacy_false_encode_not_found_is_manual_no_repeat(self):
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("legacy no-repeat must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = "film20201080pblurayx264grp"
            old = {
                "status": "NOT_FOUND",
                "terminal": True,
                "name": "Film.2020.1080p.BluRay.x264-GRP",
                "parsed": {
                    "release_type": "Encode",
                    "source": "BluRay",
                    "medium": "BluRay",
                },
                "candidate": {"processing": {"status": "READY"}},
                "processing": {"status": "READY", "ready_path": "/wrong"},
            }
            store.record_release(key, old)
            prompts = []
            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "igen",
            )
            self.assertEqual(prompts, [])
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "LEGACY_DECISION_NO_REPEAT")
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(record["previous_decision"]["status"], "NOT_FOUND")
            self.assertEqual(summary.state_counts["MANUAL"], 1)

            second = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "igen",
            )
            self.assertEqual(prompts, [])
            self.assertEqual(store.get_release(key), record)
            self.assertEqual(second.state_counts["MANUAL"], 1)

    def test_v1_aither_decisions_without_ids_are_manual_no_repeat(self):
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("v1 Aither decision must spend zero provider API")

        for old_status in ("NOT_FOUND", "DUPLICATE"):
            with self.subTest(status=old_status), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                store = StateStore(root / "state.json")
                key = norm_name("Film.2020.1080p.BluRay.x264-GRP")
                store.record_release(
                    key,
                    {
                        "status": old_status,
                        "terminal": True,
                        "decision_version": 1,
                        "candidate": {"processing": {"status": "READY"}},
                        "processing": {
                            "status": "READY",
                            "ready_path": "/unsafe",
                        },
                    },
                )
                prompts = []
                summary = scan_completed(
                    qb,
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    repair_input=lambda prompt: prompts.append(prompt) or "igen",
                )
                record = store.get_release(key)
                self.assertEqual(prompts, [])
                self.assertEqual(record["status"], "MANUAL")
                self.assertEqual(record["reason"], "LEGACY_DECISION_NO_REPEAT")
                self.assertIsNone(record["lookup"])
                self.assertIsNone(record["candidate"])
                self.assertEqual(record["processing"], {"status": "NONE"})
                self.assertEqual(record["previous_decision"]["status"], old_status)
                self.assertEqual(summary.state_counts["MANUAL"], 1)

                second = scan_completed(
                    qb,
                    Never(),
                    Never(),
                    store,
                    root / "log",
                    repair_input=lambda prompt: prompts.append(prompt) or "igen",
                )
                self.assertEqual(prompts, [])
                self.assertEqual(store.get_release(key), record)
                self.assertEqual(second.state_counts["MANUAL"], 1)

    def test_legacy_manual_without_identity_is_manual_no_repeat(self):
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("legacy MANUAL must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name("Film.2020.1080p.BluRay.x264-GRP")
            store.record_release(
                key,
                {
                    "status": "MANUAL",
                    "terminal": True,
                    "candidate": {"processing": {"status": "READY"}},
                    "processing": {"status": "READY", "ready_path": "/unsafe"},
                },
            )
            prompts = []
            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "igen",
            )
            record = store.get_release(key)
            self.assertEqual(prompts, [])
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "LEGACY_DECISION_NO_REPEAT")
            self.assertIsNone(record["lookup"])
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(record["previous_decision"]["status"], "MANUAL")
            self.assertEqual(summary.state_counts["MANUAL"], 1)

            second = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "igen",
            )
            self.assertEqual(prompts, [])
            self.assertEqual(store.get_release(key), record)
            self.assertEqual(second.state_counts["MANUAL"], 1)

    def test_unsafe_persisted_recheck_is_manual_without_prompt(self):
        qb = self._one_release_qbit()

        class Never:
            def __getattr__(inner, name):
                raise AssertionError("unsafe RECHECK must spend zero provider API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name("Film.2020.1080p.BluRay.x264-GRP")
            store.record_release(
                key,
                {
                    "status": "RECHECK_REQUIRED",
                    "terminal": True,
                    "decision_version": 3,
                    "reason": "LEGACY_V1_KNOWN_PARSER_BUG",
                    "candidate": {"processing": {"status": "READY"}},
                    "processing": {"status": "READY", "ready_path": "/unsafe"},
                    "previous_decision": {
                        "status": "MANUAL",
                        "lookup": "tmdb",
                        "tmdb_id": 123,
                    },
                },
            )
            prompts = []
            summary = scan_completed(
                qb,
                Never(),
                Never(),
                store,
                root / "log",
                repair_input=lambda prompt: prompts.append(prompt) or "igen",
            )
            record = store.get_release(key)
            self.assertEqual(prompts, [])
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(record["reason"], "LEGACY_DECISION_NO_REPEAT")
            self.assertEqual(record["lookup"], "tmdb")
            self.assertEqual(record["tmdb_id"], 123)
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_qbit_list_removal_can_never_delete_files(self):
        class Session:
            trust_env = True

            def __init__(inner):
                inner.calls = []

            def post(inner, url, data, timeout):
                inner.calls.append((url, data, timeout))
                return _FakeResponse()

        client = QbitClient("http://localhost:8080", "user", "secret")
        session = Session()
        client.session = session
        client.remove_list_only(["b", "a", "a"])
        self.assertEqual(
            session.calls[0][1], {"hashes": "a|b", "deleteFiles": "false"}
        )

    def test_qbit_stop_waits_for_confirmed_stopped_state(self):
        client = QbitClient("http://localhost:8080", "user", "secret")
        client._post_hashes = mock.Mock()
        client.torrents = mock.Mock(
            side_effect=[
                [{"hash": "a", "state": "stalledUP"}],
                [{"hash": "a", "state": "stoppedUP"}],
            ]
        )
        with mock.patch("aither_check.time.sleep") as sleeper:
            client.stop(["a"])
        client._post_hashes.assert_called_once_with("torrents/stop", ["a"])
        self.assertEqual(client.torrents.call_count, 2)
        sleeper.assert_called_once_with(0.25)

    def test_pre_aither_provider_error_does_not_consume_release(self):
        class Tmdb:
            def search(inner, parsed):
                raise ProviderError("TMDb unavailable")

        class NeverAither:
            def search_once(inner, params, **kwargs):
                raise AssertionError("Aither must not run")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            summary = scan_completed(
                self._one_release_qbit(),
                Tmdb(),
                NeverAither(),
                store,
                root / "log",
            )
            self.assertEqual(summary.errors, 1)
            self.assertEqual(store.terminal_records(), {})

    def test_attempted_aither_error_is_terminal_and_never_retried(self):
        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

        class Aither:
            def __init__(inner):
                inner.calls = 0

            def search_once(inner, params, before_request=None):
                inner.calls += 1
                before_request("a" * 64)
                raise AitherError("HTTP 429")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            aither = Aither()
            first = scan_completed(
                self._one_release_qbit(), Tmdb(), aither, store, root / "log"
            )
            second = scan_completed(
                self._one_release_qbit(), Tmdb(), aither, store, root / "log"
            )
            self.assertEqual(first.errors, 1)
            self.assertEqual(second.already_terminal, 1)
            self.assertEqual(aither.calls, 1)
            record = next(iter(store.terminal_records().values()))
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(
                record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY"
            )
            self.assertEqual(
                record["previous_decision"]["status"], "AITHER_ATTEMPTED"
            )
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})

    def test_aither_error_before_attempt_marker_is_not_falsely_spent(self):
        class Tmdb:
            def search(inner, parsed):
                return {"tmdb_id": 123, "media_type": "movie"}

        class Aither:
            def __init__(inner):
                inner.calls = 0

            def search_once(inner, params, before_request=None):
                inner.calls += 1
                raise AitherError("local validation before HTTP")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            aither = Aither()
            summary = scan_completed(
                self._one_release_qbit(), Tmdb(), aither, store, root / "log"
            )
            self.assertEqual(summary.errors, 1)
            self.assertEqual(aither.calls, 1)
            self.assertEqual(store.terminal_records(), {})

    def test_existing_aither_api_error_is_reclassified_manual_without_http(self):
        name = "Ace.Ventura.Complete.UHD.BluRay.2160p.HEVC-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [
                    {
                        "hash": "ace",
                        "name": name,
                        "state": "uploading",
                        "save_path": "/missing",
                        "content_path": "/missing/Ace",
                        "progress": 1,
                    }
                ]

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError(
                    "historical spent attempt repair must use zero provider/API"
                )

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            old = {
                "status": "API_ERROR",
                "terminal": True,
                "decision_version": 4,
                "lookup": "tmdb",
                "tmdb_id": 61418,
                "message": "Az Aither lapozasi metaadata hianyos",
                "reason": "AITHER_API_ERROR",
                "candidate": {"content_path": "/unsafe"},
                "processing": {"status": "READY", "ready_path": "/unsafe"},
            }
            store.record_release(key, old)
            first = scan_completed(
                Qbit(), Never(), Never(), store, root / "log", imdb=Never()
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(
                record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY"
            )
            self.assertEqual(record["lookup"], "tmdb")
            self.assertEqual(record["tmdb_id"], 61418)
            self.assertEqual(record["previous_decision"], old)
            self.assertIsNone(record["candidate"])
            self.assertEqual(record["processing"], {"status": "NONE"})
            self.assertEqual(first.errors, 0)
            self.assertEqual(first.state_counts["MANUAL"], 1)

            second = scan_completed(
                Qbit(), Never(), Never(), store, root / "log", imdb=Never()
            )
            self.assertEqual(second.already_terminal, 1)
            self.assertEqual(store.get_release(key), record)

    def test_legacy_reasonless_api_error_is_reclassified_without_http(self):
        name = "Legacy.Film.2020.1080p.WEB.H264-GRP"

        class Qbit:
            def torrents(inner, filter_name="all", hashes=None):
                return [{"hash": "legacy", "name": name, "progress": 1}]

        class Never:
            def __getattr__(inner, attribute):
                raise AssertionError("legacy API_ERROR repair must use zero API")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(root / "state.json")
            key = norm_name(name)
            old = {
                "status": "API_ERROR",
                "terminal": True,
                "decision_version": 2,
                "lookup": "imdb",
                "imdb_id": "tt1234567",
                "message": "legacy spent request",
            }
            store.record_release(key, old)
            summary = scan_completed(
                Qbit(), Never(), Never(), store, root / "log", imdb=Never()
            )
            record = store.get_release(key)
            self.assertEqual(record["status"], "MANUAL")
            self.assertEqual(
                record["reason"], "AITHER_ATTEMPT_FAILED_NO_RETRY"
            )
            self.assertEqual(record["imdb_id"], "tt1234567")
            self.assertEqual(record["previous_decision"], old)
            self.assertEqual(summary.state_counts["MANUAL"], 1)

    def test_tmdb_network_exception_never_exposes_api_key(self):
        class Session:
            def get(inner, url, params, timeout):
                raise requests.RequestException(
                    f"request failed: {url}?api_key={params['api_key']}"
                )

        client = TmdbClient("TOP-SECRET")
        client.session = Session()
        with self.assertRaises(ProviderError) as caught:
            client._get_json("https://api.themoviedb.org/3/test", {})
        self.assertNotIn("TOP-SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
