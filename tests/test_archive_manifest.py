from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archive_manifest import (
    ListedEntry,
    expand_archive_manifest,
    find_archive_groups,
    parse_slt_listing,
)
from matching import enrich_release, parse_release

LISTING = """7-Zip 26.02 : Copyright (c) 1999-2026 Igor Pavlov

Scanning the drive for archives:
1 file, 100 bytes (1 KiB)

Listing archive: release.rar

--
Path = release.rar
Type = Rar
Physical Size = 100

----------
Path = BDMV
Folder = +
Size =
Packed Size =

Path = BDMV/STREAM
Attributes = D_ drwxr-xr-x
Size =

Path = BDMV/STREAM/00000.m2ts
Folder = -
Size = 1000
Packed Size = 900

Path = BDMV/index.bdmv
Folder = -
Size = 168

Path = CERTIFICATE/id.bdmv
Folder = -
Size = 20
"""


def touch(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


class ParseListingTests(unittest.TestCase):
    def test_files_are_listed_and_directories_dropped(self):
        entries = parse_slt_listing(LISTING)
        self.assertEqual(
            entries,
            (
                ListedEntry("BDMV/STREAM/00000.m2ts", 1000),
                ListedEntry("BDMV/index.bdmv", 168),
                ListedEntry("CERTIFICATE/id.bdmv", 20),
            ),
        )

    def test_untrustworthy_listings_are_rejected(self):
        self.assertIsNone(parse_slt_listing(""))
        self.assertIsNone(parse_slt_listing("----------\nPath = a.mkv\nSize = \n"))
        self.assertIsNone(
            parse_slt_listing("----------\nPath = a.mkv\nSize = 5\nEncrypted = +\n")
        )
        self.assertIsNone(parse_slt_listing("----------\nPath = ../a.mkv\nSize = 5\n"))
        self.assertIsNone(parse_slt_listing("----------\nPath = /abs/a.mkv\nSize = 5\n"))


class GroupTests(unittest.TestCase):
    def test_modern_and_old_style_sets_and_isos(self):
        groups = find_archive_groups(
            [
                {"name": "R/x.part02.rar"},
                {"name": "R/x.part01.rar"},
                {"name": "R/y.rar"},
                {"name": "R/y.r00"},
                {"name": "R/y.r01"},
                {"name": "R/z.iso"},
                {"name": "R/x.nfo"},
            ]
        )
        by_first = {group.first_volume: group for group in groups}
        self.assertEqual(
            set(by_first), {"R/x.part01.rar", "R/y.rar", "R/z.iso"}
        )
        self.assertEqual(len(by_first["R/y.rar"].volumes), 3)
        self.assertEqual(len(by_first["R/x.part01.rar"].volumes), 2)
        self.assertEqual(by_first["R/z.iso"].kind, "iso")

    def test_continuation_without_first_volume_is_ambiguous(self):
        self.assertIsNone(find_archive_groups([{"name": "R/y.r00"}]))


class ExpandTests(unittest.TestCase):
    def _release(self, root: Path):
        touch(root / "Rel" / "release.rar")
        touch(root / "Rel" / "release.r00")
        touch(root / "Rel" / "release.nfo")
        return [
            {"name": "Rel/release.rar", "size": 100},
            {"name": "Rel/release.r00", "size": 100},
            {"name": "Rel/release.nfo", "size": 5},
        ]

    def test_rar_volumes_are_replaced_by_their_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = self._release(root)
            seen = []

            def lister(path):
                seen.append(path.name)
                return parse_slt_listing(LISTING)

            expanded, note = expand_archive_manifest(
                {"save_path": str(root)}, files, lister=lister
            )

        self.assertTrue(note["expanded"])
        self.assertEqual(seen, ["release.rar"])
        names = sorted(item["name"] for item in expanded)
        self.assertEqual(
            names,
            [
                "Rel/BDMV/STREAM/00000.m2ts",
                "Rel/BDMV/index.bdmv",
                "Rel/CERTIFICATE/id.bdmv",
                "Rel/release.nfo",
            ],
        )

        name = "Release.2024.COMPLETE.BLURAY-GRP"
        parsed = enrich_release(parse_release(name), name, {}, expanded)
        self.assertEqual(parsed["disc_manifest_kind"], "BDMV")
        self.assertEqual(parsed["release_type"], "Full Disc")
        self.assertEqual(parsed["disc_file_count"], 3)
        self.assertEqual(parsed["disc_payload_size"], 1000 + 168 + 20)

    def test_listing_failure_keeps_the_original_file_list(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = self._release(root)
            expanded, note = expand_archive_manifest(
                {"save_path": str(root)}, files, lister=lambda path: None
            )
        self.assertFalse(note["expanded"])
        self.assertEqual(note["reason"], "listing_failed")
        self.assertEqual(expanded, files)

    def test_missing_or_symlinked_archive_is_never_listed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = [{"name": "Rel/missing.rar", "size": 1}]
            calls = []
            _, note = expand_archive_manifest(
                {"save_path": str(root)}, files, lister=lambda p: calls.append(p)
            )
            self.assertEqual(note["reason"], "archive_path_unsafe")
            outside = touch(Path(temporary) / "outside" / "real.rar")
            link = root / "inside"
            link.mkdir()
            (link / "linked.rar").symlink_to(outside)
            _, note = expand_archive_manifest(
                {"save_path": str(link)},
                [{"name": "linked.rar", "size": 1}],
                lister=lambda p: calls.append(p),
            )
            self.assertEqual(note["reason"], "archive_path_unsafe")
        self.assertEqual(calls, [])

    def test_iso_members_replace_the_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            touch(root / "Rel" / "disc.iso")
            expanded, note = expand_archive_manifest(
                {"save_path": str(root)},
                [{"name": "Rel/disc.iso", "size": 9}],
                lister=lambda path: parse_slt_listing(LISTING),
            )
        self.assertTrue(note["expanded"])
        self.assertNotIn("Rel/disc.iso", [item["name"] for item in expanded])
        self.assertIn("Rel/BDMV/index.bdmv", [item["name"] for item in expanded])

    def test_plain_torrent_is_returned_unchanged(self):
        files = [{"name": "Rel/film.mkv", "size": 10}]
        expanded, note = expand_archive_manifest({"save_path": "/nonexistent"}, files)
        self.assertEqual(expanded, files)
        self.assertEqual(note, {"expanded": False, "reason": "no_archive"})


if __name__ == "__main__":
    unittest.main()
