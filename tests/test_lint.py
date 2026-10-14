from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aither_check import collect_lint_findings
from lint import lint_many, lint_release, redundant_audio


def track(fmt, channels, language="en", **extra):
    item = {
        "@type": "Audio",
        "Format": fmt,
        "Format_Commercial_IfAny": extra.pop("commercial", fmt),
        "Channels": str(channels),
        "Language": language,
    }
    item.update(extra)
    return item


def payload(*audio):
    return {"media": {"track": [{"@type": "General"}, {"@type": "Video"}, *audio]}}


LEGEND = payload(
    track("MLP FBA", 8, commercial="Dolby TrueHD with Dolby Atmos"),
    track("AC-3", 6, commercial="Dolby Digital", ServiceKind="CM"),
    track("DTS", 6, commercial="DTS-HD Master Audio"),
)


class RedundantAudioTests(unittest.TestCase):
    def test_three_english_tracks_are_reported_with_readable_names(self):
        self.assertEqual(
            redundant_audio(LEGEND),
            ["en: TrueHD Atmos 8ch + DD 6ch + DTS-HD MA 6ch"],
        )

    def test_one_track_per_language_is_fine(self):
        self.assertEqual(
            redundant_audio(payload(track("DTS", 6, "en"), track("AC-3", 6, "fr"))), []
        )

    def test_commentary_and_audio_description_are_not_redundant(self):
        clean = payload(
            track("MLP FBA", 8, "en"),
            track("AC-3", 2, "en", Title="Director's Commentary"),
            track("AC-3", 2, "en", ServiceKind="VI"),
            track("AC-3", 2, "en", ServiceKind="C"),
            track("AC-3", 2, "en", Title="Audio Description"),
        )
        self.assertEqual(redundant_audio(clean), [])

    def test_missing_language_counts_as_one_group(self):
        both = payload(track("DTS", 6, ""), track("AC-3", 6, ""))
        self.assertEqual(len(redundant_audio(both)), 1)


class RealWorldTrackPatternTests(unittest.TestCase):
    """Track lists taken from real remuxes (CiNEPHiLES, HDH) on 2026-10-14."""

    def test_compatibility_track_next_to_the_main_track_is_redundant(self):
        demeter = payload(
            track("MLP FBA", 8, commercial="Dolby TrueHD with Dolby Atmos", Title="TrueHD Atmos 7.1"),
            track("AC-3", 6, commercial="Dolby Digital", Title="Compatibility Track 5.1", ServiceKind="CM"),
            track("AC-3", 2, commercial="Dolby Digital", Title="Commentary by director Andre Ovredal", ServiceKind="CM"),
        )
        self.assertEqual(
            redundant_audio(demeter),
            ["en: TrueHD Atmos 8ch + DD 6ch (compatibility track)"],
        )
        popes = payload(
            track("MLP FBA", 8, commercial="Dolby TrueHD with Dolby Atmos", Title="Dolby Atmos 7.1 Mix"),
            track("AC-3", 6, commercial="Dolby Digital", Title="Compatibility Track", ServiceKind="CM"),
        )
        self.assertEqual(len(redundant_audio(popes)), 1)

    def test_alternate_mixes_with_their_own_titles_are_not_redundant(self):
        monster_squad = payload(
            track("FLAC", 2, Title="Original Dolby Stereo Mix / Matrixed"),
            track("DTS", 6, commercial="DTS-HD Master Audio", Title="5.1 Surround Remix"),
            track("AC-3", 2, Title="Commentary by co-writer/director Fred Dekker", ServiceKind="CM"),
            track("FLAC", 2, "de", Title="Commentary by David Renske"),
        )
        self.assertEqual(redundant_audio(monster_squad), [])

    def test_a_lossy_extra_with_a_real_title_is_not_a_second_main_track(self):
        red_shoes = payload(
            track("FLAC", 1, Title="Original Mono Mix"),
            track("AC-3", 1, Title="Commentary by film scholar Ian Christie", ServiceKind="CM"),
            track("AC-3", 1, Title="Actor Jeremy Irons reading excerpts from the novelization", ServiceKind="CM"),
        )
        self.assertEqual(redundant_audio(red_shoes), [])

    def test_untitled_duplicates_in_several_codecs_are_redundant(self):
        # the HDH remux that Aither staff rejected as "redundant audio"
        self.assertEqual(
            redundant_audio(LEGEND),
            ["en: TrueHD Atmos 8ch + DD 6ch + DTS-HD MA 6ch"],
        )

    def test_two_untitled_or_identically_titled_lossless_tracks_are_redundant(self):
        same = payload(
            track("FLAC", 2, Title="Stereo Mix"), track("FLAC", 2, Title="Stereo Mix")
        )
        self.assertEqual(len(redundant_audio(same)), 1)


class LintReleaseTests(unittest.TestCase):
    def test_only_remuxes_are_linted_and_the_runner_gets_the_video_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "Release"
            (root / "Sample").mkdir(parents=True)
            (root / "Sample" / "sample.mkv").write_bytes(b"s" * 50)
            movie = root / "movie.mkv"
            movie.write_bytes(b"m" * 10)
            seen = []

            def runner(path):
                seen.append(path)
                return LEGEND

            findings = lint_release(root, "Remux", runner=runner)
            self.assertEqual([f.code for f in findings], ["REDUNDANT_AUDIO"])
            self.assertEqual(seen, [movie])
            self.assertEqual(lint_release(root, "Encode", runner=runner), [])
            self.assertEqual(lint_release(root, None, runner=runner), [])

    def test_discs_missing_files_and_runner_failures_are_silent(self):
        with tempfile.TemporaryDirectory() as temporary:
            disc = Path(temporary) / "Disc"
            (disc / "BDMV" / "STREAM").mkdir(parents=True)
            (disc / "BDMV" / "STREAM" / "00000.m2ts").write_bytes(b"x")
            self.assertEqual(lint_release(disc, "Remux", runner=lambda p: LEGEND), [])
            self.assertEqual(
                lint_release(Path(temporary) / "missing.mkv", "Remux", runner=lambda p: LEGEND),
                [],
            )
            movie = Path(temporary) / "m.mkv"
            movie.write_bytes(b"x")
            self.assertEqual(lint_release(movie, "Remux", runner=lambda p: None), [])

    def test_lint_many_keys_findings_by_display_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / "m.mkv"
            movie.write_bytes(b"x")
            result = lint_many([("Movie", movie, "Remux")], runner=lambda p: LEGEND)
        self.assertEqual(list(result), ["Movie"])


class CollectLintFindingsTests(unittest.TestCase):
    def test_only_ready_not_found_records_are_linted(self):
        with tempfile.TemporaryDirectory() as temporary:
            movie = Path(temporary) / "m.mkv"
            movie.write_bytes(b"x")
            snapshot = {
                "releases": {
                    "a": {
                        "name": "A",
                        "status": "NOT_FOUND",
                        "parsed": {"release_type": "Remux"},
                        "processing": {"status": "READY", "ready_path": str(movie)},
                    },
                    "b": {
                        "name": "B",
                        "status": "DUPLICATE",
                        "parsed": {"release_type": "Remux"},
                        "processing": {"status": "READY", "ready_path": str(movie)},
                    },
                    "c": {
                        "name": "C",
                        "status": "NOT_FOUND",
                        "parsed": {"release_type": "Remux"},
                        "processing": {"status": "PENDING"},
                    },
                }
            }
            found = collect_lint_findings(
                snapshot, ["a", "b", "c", "missing"], runner=lambda p: LEGEND
            )
        self.assertEqual(list(found), ["A"])


if __name__ == "__main__":
    unittest.main()
