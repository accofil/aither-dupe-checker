from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

from identity_overrides import (
    IdentityOverrideError,
    load_identity_overrides,
    verified_identity_hint,
)


DISC_IDENTITY = {
    "tmdb_id": 7,
    "imdb_id": "tt1234567",
    "media_type": "movie",
    "title": "Film",
    "year": 2020,
}
TV_IDENTITY = {
    "tmdb_id": 79012,
    "imdb_id": "tt6036728",
    "media_type": "tv",
    "title": "The Woman in White",
    "year": 2018,
}


class IdentityOverrideTests(unittest.TestCase):
    def _load(self, payload: dict) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "overrides.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            return load_identity_overrides(path)

    @staticmethod
    def _payload_entry() -> dict:
        return {
            "match": {
                "evidence_type": "media_payload",
                "media_payload_signature": [
                    {"extension": ".MKV", "size": 700},
                    {"extension": "mkv", "size": 500},
                ],
                "torrent_file_count": 3,
                "disc_total_size": 1_250,
                "torrent_infohash": "a" * 40,
            },
            "identity": dict(TV_IDENTITY),
            "payload_review": {
                "required": True,
                "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
            },
        }

    def test_exact_manifest_and_size_are_required_for_legacy_disc_override(self):
        fingerprint = "a" * 64
        overrides = {
            "opaque": {
                "match": {
                    "disc_manifest_kind": "BDMV",
                    "disc_manifest_fingerprint": fingerprint,
                    "disc_total_size": 123,
                },
                "identity": {"tmdb_id": 7, "media_type": "movie"},
            }
        }
        parsed = {
            "disc_manifest_kind": "BDMV",
            "disc_manifest_fingerprint": fingerprint,
            "disc_total_size": 123,
        }
        self.assertEqual(
            verified_identity_hint("opaque", parsed, overrides)["tmdb_id"], 7
        )
        self.assertIsNone(
            verified_identity_hint(
                "opaque", {**parsed, "disc_total_size": 124}, overrides
            )
        )
        self.assertIsNone(
            verified_identity_hint(
                "opaque", {**parsed, "disc_manifest_fingerprint": "b" * 64}, overrides
            )
        )

    def test_v1_loader_remains_disc_compatible(self):
        loaded = self._load(
            {
                "version": 1,
                "overrides": {
                    "opaque": {
                        "match": {
                            "disc_manifest_kind": "BDMV",
                            "disc_manifest_fingerprint": "a" * 64,
                            "disc_total_size": 123,
                        },
                        "identity": dict(DISC_IDENTITY),
                        "ignored": "not retained",
                    }
                },
            }
        )
        self.assertEqual(
            loaded["opaque"]["identity"]["source"], "verified_disc_override"
        )
        self.assertNotIn("ignored", loaded["opaque"])
        self.assertNotIn("evidence_type", loaded["opaque"]["match"])

    def test_v2_disc_shape_is_supported_without_changing_disc_hint(self):
        loaded = self._load(
            {
                "version": 2,
                "overrides": {
                    "opaque": {
                        "match": {
                            "evidence_type": "disc_manifest",
                            "disc_manifest_kind": "VIDEO_TS",
                            "disc_manifest_fingerprint": "b" * 64,
                            "disc_total_size": 456,
                        },
                        "identity": dict(DISC_IDENTITY),
                    }
                },
            }
        )
        parsed = {
            "disc_manifest_kind": "VIDEO_TS",
            "disc_manifest_fingerprint": "b" * 64,
            "disc_total_size": 456,
        }
        hint = verified_identity_hint("opaque", parsed, loaded)
        self.assertEqual(hint["source"], "verified_disc_override")
        self.assertNotIn("payload_review", hint)

    def test_exact_media_payload_returns_identity_and_review_marker(self):
        loaded = self._load(
            {
                "version": 2,
                "overrides": {"thewomaninwhite": self._payload_entry()},
            }
        )
        self.assertEqual(
            loaded["thewomaninwhite"]["match"]["media_payload_signature"],
            (("mkv", 500), ("mkv", 700)),
        )
        self.assertEqual(
            loaded["thewomaninwhite"]["match"]["torrent_infohash"], "a" * 40
        )
        parsed = {
            # Runtime enrichment produces tuple pairs; ordering is not identity.
            "media_payload_signature": (("MKV", 700), (".mkv", 500)),
            "torrent_file_count": 3,
            "disc_total_size": 1_250,
        }
        hint = verified_identity_hint(
            "thewomaninwhite", parsed, loaded, torrent_hash="A" * 40
        )
        self.assertEqual(hint["tmdb_id"], 79012)
        self.assertEqual(hint["imdb_id"], "tt6036728")
        self.assertEqual(hint["source"], "verified_media_payload_override")
        self.assertEqual(
            hint["payload_review"],
            {
                "required": True,
                "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
            },
        )

    def test_media_payload_any_evidence_mismatch_fails_closed(self):
        loaded = self._load(
            {
                "version": 2,
                "overrides": {"thewomaninwhite": self._payload_entry()},
            }
        )
        parsed = {
            "media_payload_signature": (("mkv", 500), ("mkv", 700)),
            "torrent_file_count": 3,
            "disc_total_size": 1_250,
        }
        mismatches = (
            {**parsed, "media_payload_signature": (("mkv", 500), ("mkv", 701))},
            {**parsed, "media_payload_signature": (("mkv", 500), ("mp4", 700))},
            {**parsed, "media_payload_signature": (("mkv", 500),)},
            {**parsed, "torrent_file_count": 2},
            {**parsed, "disc_total_size": 1_251},
            {**parsed, "disc_manifest_kind": "BDMV"},
        )
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                self.assertIsNone(
                    verified_identity_hint(
                        "thewomaninwhite", mismatch, loaded, torrent_hash="a" * 40
                    )
                )

        for missing in (
            "media_payload_signature",
            "torrent_file_count",
            "disc_total_size",
        ):
            with self.subTest(missing=missing):
                incomplete = dict(parsed)
                incomplete.pop(missing)
                self.assertIsNone(
                    verified_identity_hint(
                        "thewomaninwhite", incomplete, loaded, torrent_hash="a" * 40
                    )
                )

        self.assertIsNone(verified_identity_hint("thewomaninwhite", parsed, loaded))
        self.assertIsNone(
            verified_identity_hint(
                "thewomaninwhite", parsed, loaded, torrent_hash="b" * 40
            )
        )
        self.assertIsNone(
            verified_identity_hint(
                "thewomaninwhite", parsed, loaded, torrent_hash="not-an-infohash"
            )
        )

    def test_media_payload_runtime_types_fail_closed(self):
        loaded = self._load(
            {
                "version": 2,
                "overrides": {"thewomaninwhite": self._payload_entry()},
            }
        )
        base = {
            "media_payload_signature": (("mkv", 500), ("mkv", 700)),
            "torrent_file_count": 3,
            "disc_total_size": 1_250,
        }
        invalid = (
            {**base, "torrent_file_count": "3"},
            {**base, "torrent_file_count": True},
            {**base, "disc_total_size": "1250"},
            {**base, "media_payload_signature": (("mkv", "500"), ("mkv", 700))},
            {**base, "media_payload_signature": "mkv:500,mkv:700"},
        )
        for parsed in invalid:
            with self.subTest(parsed=parsed):
                self.assertIsNone(
                    verified_identity_hint(
                        "thewomaninwhite", parsed, loaded, torrent_hash="a" * 40
                    )
                )

    def test_loader_rejects_weak_or_ambiguous_payload_evidence(self):
        base = self._payload_entry()
        invalid_matches = []

        empty_signature = copy.deepcopy(base["match"])
        empty_signature["media_payload_signature"] = []
        invalid_matches.append(empty_signature)

        bad_extension = copy.deepcopy(base["match"])
        bad_extension["media_payload_signature"][0]["extension"] = "nfo"
        invalid_matches.append(bad_extension)

        bool_size = copy.deepcopy(base["match"])
        bool_size["media_payload_signature"][0]["size"] = True
        invalid_matches.append(bool_size)

        missing_field = copy.deepcopy(base["match"])
        missing_field.pop("torrent_file_count")
        invalid_matches.append(missing_field)

        missing_infohash = copy.deepcopy(base["match"])
        missing_infohash.pop("torrent_infohash")
        invalid_matches.append(missing_infohash)

        mixed_disc = copy.deepcopy(base["match"])
        mixed_disc["disc_manifest_fingerprint"] = "a" * 64
        invalid_matches.append(mixed_disc)

        bad_infohash = copy.deepcopy(base["match"])
        bad_infohash["torrent_infohash"] = "g" * 40
        invalid_matches.append(bad_infohash)

        short_infohash = copy.deepcopy(base["match"])
        short_infohash["torrent_infohash"] = "a" * 39
        invalid_matches.append(short_infohash)

        too_few_files = copy.deepcopy(base["match"])
        too_few_files["torrent_file_count"] = 1
        invalid_matches.append(too_few_files)

        too_small_total = copy.deepcopy(base["match"])
        too_small_total["disc_total_size"] = 1_199
        invalid_matches.append(too_small_total)

        for match in invalid_matches:
            with self.subTest(match=match):
                entry = self._payload_entry()
                entry["match"] = match
                with self.assertRaises(IdentityOverrideError):
                    self._load(
                        {
                            "version": 2,
                            "overrides": {"thewomaninwhite": entry},
                        }
                    )

    def test_payload_evidence_requires_v2_and_an_explicit_type(self):
        entry = self._payload_entry()
        with self.assertRaises(IdentityOverrideError):
            self._load({"version": 1, "overrides": {"thewomaninwhite": entry}})

        entry = self._payload_entry()
        entry["match"].pop("evidence_type")
        with self.assertRaises(IdentityOverrideError):
            self._load({"version": 2, "overrides": {"thewomaninwhite": entry}})

    def test_payload_review_marker_is_strict_and_media_only(self):
        invalid_reviews = (
            None,
            {"required": False, "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD"},
            {"required": True, "manual_reason": "human prose"},
            {"required": True},
            {
                "required": True,
                "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
                "extra": True,
            },
        )
        for review in invalid_reviews:
            with self.subTest(review=review):
                entry = self._payload_entry()
                entry["payload_review"] = review
                with self.assertRaises(IdentityOverrideError):
                    self._load(
                        {
                            "version": 2,
                            "overrides": {"thewomaninwhite": entry},
                        }
                    )

        disc_entry = {
            "match": {
                "evidence_type": "disc_manifest",
                "disc_manifest_kind": "BDMV",
                "disc_manifest_fingerprint": "a" * 64,
                "disc_total_size": 123,
            },
            "identity": dict(DISC_IDENTITY),
            "payload_review": {
                "required": True,
                "manual_reason": "OVERLAPPING_EPISODE_PAYLOAD",
            },
        }
        with self.assertRaises(IdentityOverrideError):
            self._load({"version": 2, "overrides": {"opaque": disc_entry}})

    def test_v2_entry_fields_and_tmdb_type_are_strict(self):
        for tmdb_id in (True, 79012.0, "79012"):
            with self.subTest(tmdb_id=tmdb_id):
                entry = self._payload_entry()
                entry["identity"]["tmdb_id"] = tmdb_id
                with self.assertRaises(IdentityOverrideError):
                    self._load(
                        {
                            "version": 2,
                            "overrides": {"thewomaninwhite": entry},
                        }
                    )

        entry = self._payload_entry()
        entry["payload_reveiw"] = entry.pop("payload_review")
        with self.assertRaises(IdentityOverrideError):
            self._load(
                {"version": 2, "overrides": {"thewomaninwhite": entry}}
            )

        entry = self._payload_entry()
        entry["ignored"] = "v2 must not silently discard this"
        with self.assertRaises(IdentityOverrideError):
            self._load(
                {"version": 2, "overrides": {"thewomaninwhite": entry}}
            )

        disc_entry = {
            "match": {
                "evidence_type": "disc_manifest",
                "disc_manifest_kind": "BDMV",
                "disc_manifest_fingerprint": "a" * 64,
                "disc_total_size": "123",
            },
            "identity": dict(DISC_IDENTITY),
        }
        with self.assertRaises(IdentityOverrideError):
            self._load({"version": 2, "overrides": {"opaque": disc_entry}})

    def test_unsupported_schema_is_rejected(self):
        for version in (True, 0, 3, "2"):
            with self.subTest(version=version):
                with self.assertRaises(IdentityOverrideError):
                    self._load({"version": version, "overrides": {}})


if __name__ == "__main__":
    unittest.main()
