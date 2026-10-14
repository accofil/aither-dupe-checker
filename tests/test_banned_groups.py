from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

import matching
from banned_groups import (
    BannedGroups,
    fetch_banned_groups,
    load_banned_groups,
    parse_banned_groups,
)


def raw(count=25):
    items = [{"name": f"Group{index}", "types": "all", "reason": "Quality"} for index in range(count)]
    items += [
        {"name": "ByType", "types": {"types": ["2", "5"]}, "reason": "x"},
        {"name": "NoType", "types": {"types": []}, "reason": "Retagging"},
        {"name": "UnknownType", "types": {"types": ["99"]}, "reason": "x"},
        {"name": "UnKn0wn", "types": "all", "reason": "Quality"},
    ]
    return items


class ParseTests(unittest.TestCase):
    def test_types_are_mapped_to_the_three_blacklist_shapes(self):
        groups = parse_banned_groups(raw(2))
        self.assertIn("group0", groups.all_types)
        self.assertIn("unkn0wn", groups.all_types)
        self.assertEqual(groups.by_type["bytype"], frozenset({"Remux", "WEBRip"}))
        # no type, or only ids we do not know, keeps Aither's conservative "all"
        self.assertEqual(groups.no_type_given, frozenset({"notype", "unknowntype"}))

    def test_junk_records_are_ignored(self):
        groups = parse_banned_groups([{"types": "all"}, {"name": ""}, "x", {"name": " A "}])
        self.assertEqual(groups.size, 1)
        self.assertIn("a", groups.no_type_given)


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        return self.pages.pop(0)


class FetchTests(unittest.TestCase):
    def test_follows_the_cursor_and_sends_the_bearer_token(self):
        session = FakeSession(
            [
                FakeResponse(200, {"data": [{"name": "A"}], "meta": {"next_cursor": "c2"}}),
                FakeResponse(200, {"data": [{"name": "B"}], "meta": {"next_cursor": None}}),
            ]
        )
        items = fetch_banned_groups("https://aither.cc/", "tok", session=session)
        self.assertEqual([item["name"] for item in items], ["A", "B"])
        self.assertEqual(session.calls[0][0], "https://aither.cc/api/blacklists/releasegroups")
        self.assertEqual(session.calls[1][1]["cursor"], "c2")
        self.assertEqual(session.calls[0][2]["Authorization"], "Bearer tok")

    def test_any_failure_returns_none_never_a_partial_list(self):
        for pages in (
            [FakeResponse(500, {})],
            [FakeResponse(200, ValueError("bad"))],
            [FakeResponse(200, {"data": "x"})],
            [
                FakeResponse(200, {"data": [{"name": "A"}], "meta": {"next_cursor": "c"}}),
                FakeResponse(429, {}),
            ],
        ):
            self.assertIsNone(fetch_banned_groups("https://aither.cc", "t", session=FakeSession(pages)))


class LoadTests(unittest.TestCase):
    def test_fresh_cache_needs_no_request_and_old_cache_is_refreshed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "sub" / "banned.json"

            def fetch(base, token):
                fetch.calls += 1
                return raw()

            fetch.calls = 0
            groups, source = load_banned_groups(cache, "b", "t", now=1000.0, fetch=fetch)
            self.assertEqual((source, fetch.calls), ("api", 1))
            self.assertTrue(groups.size >= 20)
            self.assertEqual(oct(cache.stat().st_mode & 0o777), "0o600")

            _, source = load_banned_groups(cache, "b", "t", now=1000.0 + 3600, fetch=fetch)
            self.assertEqual((source, fetch.calls), ("cache", 1))

            _, source = load_banned_groups(cache, "b", "t", now=1000.0 + 90000, fetch=fetch)
            self.assertEqual((source, fetch.calls), ("api", 2))

    def test_outage_falls_back_to_a_stale_cache_then_to_none(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "banned.json"
            cache.write_text(json.dumps({"fetched_at": 1.0, "raw_data": raw()}), encoding="utf-8")
            groups, source = load_banned_groups(cache, "b", "t", now=10**7, fetch=lambda b, t: None)
            self.assertEqual(source, "stale-cache")
            self.assertTrue(groups.size >= 20)

            empty = Path(temporary) / "none.json"
            groups, source = load_banned_groups(empty, "b", "t", now=1.0, fetch=lambda b, t: None)
            self.assertEqual((groups, source), (None, "none"))

    def test_an_implausibly_small_list_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as temporary:
            groups, source = load_banned_groups(
                Path(temporary) / "b.json", "b", "t", now=1.0, fetch=lambda b, t: raw(0)[:2]
            )
            self.assertEqual((groups, source), (None, "none"))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self._saved = (
            set(matching.BLACKLIST_ALL),
            set(matching.BLACKLIST_NO_TYPE_GIVEN),
            copy.deepcopy(matching.BLACKLIST_BY_TYPE),
        )

    def tearDown(self):
        matching.BLACKLIST_ALL.clear()
        matching.BLACKLIST_ALL.update(self._saved[0])
        matching.BLACKLIST_NO_TYPE_GIVEN.clear()
        matching.BLACKLIST_NO_TYPE_GIVEN.update(self._saved[1])
        matching.BLACKLIST_BY_TYPE.clear()
        matching.BLACKLIST_BY_TYPE.update(self._saved[2])

    def test_applied_list_replaces_the_static_snapshot(self):
        before, *_ = matching.is_blacklisted_release("Film.2020.1080p.BluRay.x264-NEWBAN")
        self.assertFalse(before)
        matching.apply_banned_groups(
            BannedGroups(
                frozenset({"newban"}),
                frozenset(),
                {"onlywebdl": frozenset({"WEB-DL"})},
            )
        )
        blocked, group, rule, _ = matching.is_blacklisted_release(
            "Film.2020.1080p.BluRay.x264-NEWBAN"
        )
        self.assertTrue(blocked)
        self.assertEqual(rule, "ALL")
        # an old static ban that Aither no longer lists is gone
        self.assertFalse(matching.is_blacklisted_release("Film.2020.1080p.BluRay.x264-RARBG")[0])
        # type-specific ban only hits that type
        self.assertTrue(
            matching.is_blacklisted_release("Show.S01E01.1080p.WEB-DL.H264-ONLYWEBDL")[0]
        )
        self.assertFalse(
            matching.is_blacklisted_release("Film.2020.1080p.BluRay.x264-ONLYWEBDL")[0]
        )


class StartupSyncTests(unittest.TestCase):
    """The wiring in aither_check (an unimported name once slipped past unit tests)."""

    def setUp(self):
        self._saved = (
            set(matching.BLACKLIST_ALL),
            set(matching.BLACKLIST_NO_TYPE_GIVEN),
            copy.deepcopy(matching.BLACKLIST_BY_TYPE),
        )

    def tearDown(self):
        matching.BLACKLIST_ALL.clear()
        matching.BLACKLIST_ALL.update(self._saved[0])
        matching.BLACKLIST_NO_TYPE_GIVEN.clear()
        matching.BLACKLIST_NO_TYPE_GIVEN.update(self._saved[1])
        matching.BLACKLIST_BY_TYPE.clear()
        matching.BLACKLIST_BY_TYPE.update(self._saved[2])

    def _config(self, sync=True):
        from aither_check import Config

        return Config(
            qb_url="http://127.0.0.1:1",
            qb_user="u",
            qb_pass="p",
            aither_token="t",
            banned_groups_file=Path("/nonexistent/cache.json"),
            sync_banned_groups=sync,
        )

    def test_remote_list_is_applied_and_reported(self):
        import aither_check
        from unittest import mock

        groups = BannedGroups(frozenset({"startupban"}), frozenset(), {})
        with mock.patch.object(
            aither_check, "load_banned_groups", return_value=(groups, "api")
        ):
            self.assertEqual(aither_check.sync_remote_banned_groups(self._config()), "api")
        self.assertEqual(matching.BLACKLIST_ALL, {"startupban"})

    def test_unavailable_list_keeps_the_static_snapshot(self):
        import aither_check
        from unittest import mock

        before = set(matching.BLACKLIST_ALL)
        with mock.patch.object(
            aither_check, "load_banned_groups", return_value=(None, "none")
        ):
            self.assertEqual(aither_check.sync_remote_banned_groups(self._config()), "static")
        self.assertEqual(matching.BLACKLIST_ALL, before)

    def test_sync_can_be_switched_off(self):
        import aither_check
        from unittest import mock

        with mock.patch.object(aither_check, "load_banned_groups") as loader:
            self.assertEqual(
                aither_check.sync_remote_banned_groups(self._config(sync=False)), "disabled"
            )
        loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
