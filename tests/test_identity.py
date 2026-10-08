from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from identity import (
    ContainerMetadataEvidence,
    IMDbSuggestionClient,
    IMDbSuggestionError,
    TitleVariant,
    canonical_title,
    classify_iso_listing,
    classify_manifest,
    detect_source_aither,
    release_identity_variants,
    scan_local_metadata,
    scan_matroska_metadata,
    title_alias_variants,
    _parse_ffprobe_container_metadata,
    _probe_matroska_path,
)


class SourceDetectionTests(unittest.TestCase):
    def test_tracker_and_comment_hosts_are_detected_without_returning_url(self):
        self.assertIs(
            detect_source_aither(
                {"tracker": "https://announce.aither.cc/secret-passkey/announce"}
            ),
            True,
        )
        self.assertIs(
            detect_source_aither(
                {"comment": "Release page: https://aither.cc/torrents/123"}
            ),
            True,
        )
        self.assertIs(
            detect_source_aither(
                {}, [{"url": "https://tracker.aither.cc/x/announce"}]
            ),
            True,
        )

    def test_hostname_boundary_rejects_spoofs_and_plain_text(self):
        for value in (
            "https://aither.cc.evil.invalid/announce",
            "https://not-aither.cc/announce",
            "Aither.cc release",
            "https://evil.invalid/?next=https://aither.cc/secret",
        ):
            with self.subTest(value=value):
                self.assertFalse(detect_source_aither({"tracker": value}))


class ManifestTests(unittest.TestCase):
    def test_video_disc_archive_and_iso_remain_media_candidates(self):
        cases = (
            [{"name": "Movie/movie.mkv"}],
            [{"name": "DISC/BDMV/STREAM/00000.m2ts"}],
            [{"name": "Movie.iso"}],
            [{"name": "Movie.part01.rar"}],
        )
        for files in cases:
            with self.subTest(files=files):
                self.assertEqual(classify_manifest(files), "MEDIA")

    def test_positive_non_video_payload_is_not_media(self):
        files = [
            {"name": "Game/setup.exe"},
            {"name": "Game/steam_api64.dll"},
            {"name": "Game/README.txt"},
        ]
        self.assertEqual(classify_manifest(files), "NOT_MEDIA")
        self.assertEqual(
            classify_manifest(
                [{"name": "Game/setup.exe"}, {"name": "Game/setup-1.bin"}]
            ),
            "NOT_MEDIA",
        )
        self.assertEqual(classify_manifest([{"name": "cover.jpg"}]), "NOT_MEDIA")

    def test_empty_or_unknown_payload_is_not_guessed(self):
        self.assertEqual(classify_manifest([]), "UNKNOWN")
        self.assertEqual(classify_manifest([{"name": "payload.data"}]), "UNKNOWN")
        self.assertEqual(classify_manifest([{"name": "../movie.mkv"}]), "UNKNOWN")


class IsoListingTests(unittest.TestCase):
    def test_bluray_iso_is_media(self):
        listing = """\
Path = /downloads/Movie.iso
Type = Udf
----------
Path = BDMV
Folder = +

Path = BDMV\\STREAM\\00000.M2TS
Size = 40000000000
"""
        self.assertEqual(classify_iso_listing(listing), "MEDIA")

    def test_dvd_iso_is_media(self):
        listing = """\
----------
Path = VIDEO_TS/VIDEO_TS.IFO
Size = 12288

Path = VIDEO_TS/VTS_01_1.VOB
Size = 1073709056
"""
        self.assertEqual(classify_iso_listing(listing), "MEDIA")

    def test_game_iso_is_not_media(self):
        listing = """\
Path = Game.iso
Type = Udf
----------
Path = setup.exe
Size = 1200000

Path = data/game-1.bin
Size = 8000000000

Path = engine/content.pak
Size = 500000000
"""
        self.assertEqual(classify_iso_listing(listing), "NOT_MEDIA")

    def test_ambiguous_iso_remains_unknown(self):
        listing = """\
----------
Path = README.txt
Size = 2000

Path = payload.dat
Size = 999999
"""
        self.assertEqual(classify_iso_listing(listing), "UNKNOWN")
        self.assertEqual(classify_iso_listing("7-Zip error: cannot open"), "UNKNOWN")

    def test_video_evidence_wins_over_game_like_sidecars(self):
        listing = """\
----------
Path = extras/launcher.exe
Path = feature/movie.mkv
"""
        self.assertEqual(classify_iso_listing(listing), "MEDIA")


class MetadataScanTests(unittest.TestCase):
    def test_reads_unique_imdb_id_and_bdmv_meta_title(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nfo = root / "Release" / "movie.nfo"
            xml = root / "Release" / "BDMV" / "META" / "DL" / "bdmt_eng.xml"
            nfo.parent.mkdir(parents=True)
            xml.parent.mkdir(parents=True)
            nfo.write_text("https://www.imdb.com/title/tt0123456/", encoding="utf-8")
            xml.write_text(
                '<?xml version="1.0"?><di:discinfo xmlns:di="urn:test">'
                "<di:title><di:name>Godzilla &amp; Friends</di:name></di:title>"
                "</di:discinfo>",
                encoding="utf-8",
            )
            evidence = scan_local_metadata(
                {"save_path": temporary, "name": "Release"},
                [
                    {"name": "Release/movie.nfo", "size": nfo.stat().st_size},
                    {
                        "name": "Release/BDMV/META/DL/bdmt_eng.xml",
                        "size": xml.stat().st_size,
                    },
                ],
            )
        self.assertEqual(evidence.imdb_id, "tt0123456")
        self.assertEqual(evidence.imdb_ids, ("tt0123456",))
        self.assertEqual(evidence.bdmv_titles, ("Godzilla & Friends",))
        self.assertEqual(evidence.scanned_files, 2)

    def test_path_traversal_oversize_and_unlisted_symlink_are_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "downloads"
            root.mkdir()
            outside = base / "secret.nfo"
            outside.write_text("tt9999999", encoding="utf-8")
            large = root / "large.nfo"
            large.write_text("tt8888888" + "x" * 100, encoding="utf-8")
            files = [
                {"name": "../secret.nfo", "size": outside.stat().st_size},
                {"name": str(outside), "size": outside.stat().st_size},
                {"name": "large.nfo", "size": large.stat().st_size},
            ]
            link = root / "link.nfo"
            try:
                link.symlink_to(outside)
            except OSError:
                pass
            else:
                files.append({"name": "link.nfo", "size": outside.stat().st_size})
            evidence = scan_local_metadata(
                {"save_path": str(root)},
                files,
                max_file_bytes=32,
            )
        self.assertEqual(evidence.imdb_ids, ())
        self.assertEqual(evidence.scanned_files, 0)

    def test_conflicting_ids_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "a.nfo"
            second = root / "b.txt"
            first.write_text("tt0123456", encoding="utf-8")
            second.write_text("tt0654321", encoding="utf-8")
            evidence = scan_local_metadata(
                {"save_path": temporary},
                [
                    {"name": "a.nfo", "size": first.stat().st_size},
                    {"name": "b.txt", "size": second.stat().st_size},
                ],
            )
        self.assertIsNone(evidence.imdb_id)
        self.assertEqual(evidence.imdb_ids, ("tt0123456", "tt0654321"))

    def test_name_imdb_id_survives_missing_save_root_without_io(self):
        evidence = scan_local_metadata(
            {"save_path": "definitely/missing", "name": "Film.tt0123456.1080p"},
            [],
        )
        self.assertEqual(evidence.imdb_id, "tt0123456")
        self.assertEqual(evidence.scanned_files, 0)

    def test_ffprobe_tags_recover_exact_movie_ids_and_aka_title(self):
        evidence = _parse_ffprobe_container_metadata(
            b'{"format":{"tags":{'
            b'"TITLE":"\\u80ed\\u8102\\u6263 AKA Rouge (1987)",'
            b'"IMDB":"https://www.imdb.com/title/tt0093258/",'
            b'"TMDB":"movie/68883"}}}'
        )

        self.assertEqual(evidence.imdb_id, "tt0093258")
        self.assertEqual(evidence.tmdb_id, 68883)
        self.assertEqual(evidence.titles, ("\u80ed\u8102\u6263 AKA Rouge (1987)",))
        self.assertEqual(evidence.series_titles, ())

    def test_ffprobe_episode_title_is_reduced_to_convergent_series_title(self):
        evidence = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{"title":"The Woman in White S01E04 Finale"}}}'
        )

        self.assertEqual(evidence.titles, ("The Woman in White",))
        self.assertEqual(evidence.series_titles, ("The Woman in White",))

    def test_container_scanner_merges_only_safe_listed_regular_mkvs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "Release"
            release.mkdir()
            first = release / "e01.mkv"
            second = release / "e02.mkv"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            probe = root / "ffprobe"
            probe.write_bytes(b"probe")
            probe.chmod(0o700)
            returned = ContainerMetadataEvidence(
                None,
                (),
                None,
                (),
                (),
                ("The Woman in White",),
                ("The Woman in White",),
                1,
            )
            with mock.patch(
                "identity._probe_matroska_path", return_value=returned
            ) as run_probe:
                evidence = scan_matroska_metadata(
                    {"save_path": str(root)},
                    [
                        {"name": "Release/e01.mkv", "size": 3},
                        {"name": "Release/e02.mkv", "size": 3},
                        {"name": "../outside.mkv", "size": 3},
                    ],
                    ffprobe_path=probe,
                )

        self.assertEqual(evidence.titles, ("The Woman in White",))
        self.assertEqual(evidence.probed_files, 2)
        self.assertEqual(run_probe.call_count, 2)

    def test_conflicting_container_ids_remain_ambiguous(self):
        evidence = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{"IMDB":"tt0093258 tt2420038",'
            '"TMDB":"movie/68883 and movie/278276"}}}'
        )

        self.assertIsNone(evidence.imdb_id)
        self.assertEqual(evidence.imdb_ids, ("tt0093258", "tt2420038"))
        self.assertIsNone(evidence.tmdb_id)

    def test_arbitrary_bare_imdb_mentions_are_not_catalogue_evidence(self):
        ignored = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{"COMMENT":"review tt0093258 later",'
            '"ENCODER_SETTINGS":"source=tt2420038",'
            '"BAD1":"https://notimdb.com/title/tt0093258/",'
            '"BAD2":"https://fakeimdb.com/title/tt2420038/",'
            '"BAD3":"prefiximdb.com/title/tt6036728"}}}'
        )
        explicit = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{"IMDB":"tt0093258",'
            '"COMMENT":"https://www.imdb.com/title/tt0093258/"}}}'
        )

        self.assertEqual(ignored.imdb_ids, ())
        self.assertEqual(explicit.imdb_id, "tt0093258")

    def test_container_identity_requires_a_complete_bounded_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "ffprobe"
            probe.write_bytes(b"probe")
            probe.chmod(0o700)
            files = []
            for index in range(9):
                path = root / f"e{index:02d}.mkv"
                path.write_bytes(b"x")
                files.append({"name": path.name, "size": 1})
            returned = ContainerMetadataEvidence(
                "tt0093258", ("tt0093258",), None, (), (), (), (), 1
            )
            with mock.patch(
                "identity._probe_matroska_path", return_value=returned
            ) as run_probe:
                evidence = scan_matroska_metadata(
                    {"save_path": str(root)},
                    files,
                    max_files=8,
                    ffprobe_path=probe,
                )

        self.assertEqual(evidence.imdb_ids, ())
        run_probe.assert_not_called()

    def test_one_failed_probe_invalidates_the_container_identity_sample(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            probe = root / "ffprobe"
            probe.write_bytes(b"probe")
            probe.chmod(0o700)
            files = []
            for name in ("e01.mkv", "e02.mkv"):
                path = root / name
                path.write_bytes(b"x")
                files.append({"name": name, "size": 1})
            valid = ContainerMetadataEvidence(
                "tt0093258", ("tt0093258",), None, (), (), (), (), 1
            )
            empty = ContainerMetadataEvidence(None, (), None, (), (), (), (), 0)
            with mock.patch(
                "identity._probe_matroska_path", side_effect=(valid, empty)
            ):
                evidence = scan_matroska_metadata(
                    {"save_path": str(root)}, files, ffprobe_path=probe
                )

        self.assertEqual(evidence.imdb_ids, ())

    def test_in_root_intermediate_symlink_cannot_select_another_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "Other"
            other.mkdir()
            (other / "e.mkv").write_bytes(b"x")
            (root / "Release").symlink_to(other, target_is_directory=True)
            probe = root / "ffprobe"
            probe.write_bytes(b"probe")
            probe.chmod(0o700)
            returned = ContainerMetadataEvidence(
                "tt0093258", ("tt0093258",), None, (), (), (), (), 1
            )
            with mock.patch(
                "identity._probe_matroska_path", return_value=returned
            ) as run_probe:
                evidence = scan_matroska_metadata(
                    {"save_path": str(root)},
                    [{"name": "Release/e.mkv", "size": 1}],
                    ffprobe_path=probe,
                )

        self.assertEqual(evidence.imdb_ids, ())
        run_probe.assert_not_called()

    def test_same_tmdb_number_in_movie_and_tv_namespaces_is_ambiguous(self):
        evidence = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{"TMDB":'
            '"https://www.themoviedb.org/movie/123 and '
            'https://www.themoviedb.org/tv/123"}}}'
        )

        self.assertEqual(evidence.tmdb_ids, (123,))
        self.assertIsNone(evidence.tmdb_id)
        self.assertEqual(evidence.tmdb_refs, (("movie", 123), ("tv", 123)))

    def test_catalogue_url_hosts_require_an_exact_boundary(self):
        evidence = _parse_ffprobe_container_metadata(
            '{"format":{"tags":{'
            '"COMMENT":"https://fakethemoviedb.org/movie/68883 '
            'https://notimdb.com/title/tt0093258/"}}}'
        )

        self.assertEqual(evidence.tmdb_refs, ())
        self.assertEqual(evidence.imdb_ids, ())

    @unittest.skipUnless(os.name == "posix", "requires /proc/self/fd semantics")
    def test_probe_rechecks_opened_target_under_expected_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            payload = outside / "payload.mkv"
            payload.write_bytes(b"unsafe")
            (root / "swapped").symlink_to(outside, target_is_directory=True)

            evidence = _probe_matroska_path(
                root / "swapped" / "payload.mkv",
                expected_root=root.resolve(),
                expected_size=payload.stat().st_size,
                executable=Path("/bin/false"),
                timeout=1.0,
                max_output_bytes=1024,
            )

        self.assertEqual(evidence.probed_files, 0)

    @unittest.skipUnless(os.name == "posix", "requires /proc/self/fd semantics")
    def test_probe_rejects_in_root_fd_target_swap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            expected = root / "expected.mkv"
            alternate = root / "alternate.mkv"
            expected.write_bytes(b"same")
            alternate.write_bytes(b"same")
            real_open = os.open
            with mock.patch(
                "identity.os.open",
                side_effect=lambda _path, flags: real_open(alternate, flags),
            ):
                evidence = _probe_matroska_path(
                    expected,
                    expected_root=root,
                    expected_size=4,
                    executable=Path("/bin/false"),
                    timeout=1.0,
                    max_output_bytes=1024,
                )

        self.assertEqual(evidence.probed_files, 0)


class TitleVariantTests(unittest.TestCase):
    def test_bilingual_title_yields_both_exact_aliases(self):
        variants = title_alias_variants("Dead of Winter - Eisige Stille", 2025)
        self.assertEqual(variants[:2], (
            TitleVariant("Dead of Winter", 2025),
            TitleVariant("Eisige Stille", 2025),
        ))

    def test_three_market_titles_remain_separate_aliases(self):
        variants = release_identity_variants(
            "Starfight.-.Sieger.ueber.tausend.Sonnen.-.The.Last.Starfighter."
            "1984.1080p.BluRay.AVC-GRP",
            "Starfight Sieger ueber tausend Sonnen The Last Starfighter",
            1984,
        )
        self.assertEqual(
            variants,
            (
                TitleVariant("Starfight", 1984),
                TitleVariant("Sieger ueber tausend Sonnen", 1984),
                TitleVariant("The Last Starfighter", 1984),
            ),
        )
        self.assertNotIn("4K", variants[-1].title)
        self.assertNotIn("Remastered", variants[-1].title)

    def test_plain_multi_dash_catalog_title_is_not_split_into_fragments(self):
        variants = title_alias_variants("One - Two - Three", 2020)
        self.assertEqual(variants, (TitleVariant("One - Two - Three", 2020),))

    def test_parenthetical_alias_and_noise(self):
        variants = title_alias_variants("The Crimson Pirate (Il corsaro rosso)", 1952)
        self.assertIn(TitleVariant("The Crimson Pirate", 1952), variants)
        self.assertIn(TitleVariant("Il corsaro rosso", 1952), variants)
        noisy = title_alias_variants("Film (Remastered)", 1999)
        self.assertNotIn(TitleVariant("Remastered", 1999), noisy)

    def test_multi_feature_keeps_each_own_year(self):
        variants = title_alias_variants(
            "Blast-Off Girls (1967) & The Girl, the Body, and the Pill (1967)"
        )
        self.assertEqual(
            variants[:2],
            (
                TitleVariant("Blast-Off Girls", 1967),
                TitleVariant("The Girl, the Body, and the Pill", 1967),
            ),
        )

    def test_canonical_title_is_exact_but_punctuation_and_accent_stable(self):
        self.assertEqual(canonical_title("L\u00e9on: The Professional"), "leon the professional")
        self.assertEqual(canonical_title("D.A."), "d a")

    def test_numeric_title_is_not_mistaken_for_release_year(self):
        self.assertEqual(
            title_alias_variants("1917", 2019),
            (TitleVariant("1917", 2019),),
        )
        self.assertEqual(
            title_alias_variants("2001 A Space Odyssey (1968)"),
            (TitleVariant("2001 A Space Odyssey", 1968),),
        )

    def test_release_helper_preserves_multi_feature_before_technical_tail(self):
        variants = release_identity_variants(
            "Blast-Off Girls (1967) & The Girl, the Body, and the Pill (1967) "
            "1080p USA Blu-ray AVC DTS-HD MA 2.0",
            "Blast Off Girls The Girl the Body and the Pill",
            1967,
        )
        self.assertEqual(
            variants,
            (
                TitleVariant("Blast-Off Girls", 1967),
                TitleVariant("The Girl, the Body, and the Pill", 1967),
            ),
        )

    def test_release_helper_recovers_dotted_bilingual_aliases(self):
        variants = release_identity_variants(
            "Dead.of.Winter.-.Eisige.Stille.2025.Dual.Complete.BluRay-MAMA",
            "Dead of Winter Eisige Stille",
            2025,
        )
        self.assertEqual(
            variants,
            (
                TitleVariant("Dead of Winter", 2025),
                TitleVariant("Eisige Stille", 2025),
            ),
        )

    def test_release_helper_splits_only_standalone_aka_marker(self):
        variants = release_identity_variants(
            "Rouge.AKA.Yin.ji.kau.1987.2160p.UHD.BluRay.REMUX-TEAM",
            "Rouge AKA Yin ji kau",
            1987,
        )

        self.assertEqual(
            variants,
            (
                TitleVariant("Rouge", 1987),
                TitleVariant("Yin ji kau", 1987),
            ),
        )

    def test_aka_substring_inside_a_word_is_not_split(self):
        variants = release_identity_variants(
            "Akademia.2024.1080p.WEB-DL.H264-TEAM",
            "Akademia",
            2024,
        )

        self.assertEqual(variants, (TitleVariant("Akademia", 2024),))

    def test_non_latin_aka_side_does_not_hide_safe_ascii_alias(self):
        variants = release_identity_variants(
            "", "", 1987, ("\u80ed\u8102\u6263 AKA Rouge (1987)",)
        )

        self.assertEqual(variants, (TitleVariant("Rouge", 1987),))


class _Response:
    def __init__(self, status_code=200, payload=None, json_error=None):
        self.status_code = status_code
        self.payload = payload
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class _Session:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []
        self.trust_env = True

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class IMDbSuggestionTests(unittest.TestCase):
    def test_only_exact_title_year_and_movie_type_survives(self):
        session = _Session(
            _Response(
                payload={
                    "d": [
                        {"id": "tt0000001", "l": "Godzilla", "y": 1998, "qid": "movie"},
                        {"id": "tt0000002", "l": "Godzilla", "y": 2014, "qid": "movie"},
                        {"id": "tt0000003", "l": "Godzillas", "y": 1998, "qid": "movie"},
                        {"id": "tt0000004", "l": "Godzilla", "y": 1998, "qid": "tvSeries"},
                    ]
                }
            )
        )
        identity = IMDbSuggestionClient(session=session).search_exact(
            "Godzilla", 1998, "movie"
        )
        self.assertEqual(identity.imdb_id, "tt0000001")
        self.assertFalse(session.trust_env)
        self.assertIn("Godzilla.json", session.calls[0][0])

    def test_exact_aka_can_identify_a_localized_title(self):
        session = _Session(
            _Response(
                payload={
                    "d": [
                        {
                            "id": "tt0123456",
                            "l": "Original Title",
                            "akas": ["Eisige Stille"],
                            "y": 2025,
                            "qid": "movie",
                        }
                    ]
                }
            )
        )
        identity = IMDbSuggestionClient(session=session).search_exact(
            "Eisige.Stille", 2025, "movie"
        )
        self.assertEqual(identity.imdb_id, "tt0123456")

    def test_multiple_exact_ids_are_ambiguous(self):
        session = _Session(
            _Response(
                payload={
                    "d": [
                        {"id": "tt0000001", "l": "Same", "y": 2020, "qid": "movie"},
                        {"id": "tt0000002", "l": "Same", "y": 2020, "qid": "movie"},
                    ]
                }
            )
        )
        self.assertIsNone(
            IMDbSuggestionClient(session=session).search_exact("Same", 2020, "movie")
        )

    def test_missing_year_does_not_make_request(self):
        session = _Session(_Response(payload={"d": []}))
        self.assertIsNone(
            IMDbSuggestionClient(session=session).search_exact("Film", None, "movie")
        )
        self.assertEqual(session.calls, [])

    def test_network_and_http_errors_never_leak_url(self):
        secret_url = "https://v3.sg.media-imdb.com/suggestion/x/Secret.json"
        network = _Session(error=requests.ConnectionError(f"failed for {secret_url}"))
        with self.assertRaises(IMDbSuggestionError) as caught:
            IMDbSuggestionClient(session=network).search_exact("Secret", 2020, "movie")
        self.assertNotIn("http", str(caught.exception).casefold())
        self.assertNotIn("Secret", str(caught.exception))

        http = _Session(_Response(status_code=503, payload={}))
        with self.assertRaises(IMDbSuggestionError) as caught:
            IMDbSuggestionClient(session=http).search_exact("Secret", 2020, "movie")
        self.assertNotIn("http://", str(caught.exception).casefold())
        self.assertNotIn("https://", str(caught.exception).casefold())
        self.assertNotIn("Secret", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
