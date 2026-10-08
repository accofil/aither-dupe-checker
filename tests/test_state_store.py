from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import state_store as module


class FakeClock:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class StateStoreTests(unittest.TestCase):
    def test_release_hash_bindings_are_one_atomic_preserving_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = module.StateStore(path)
            for key in ("first", "second"):
                store.record_release(
                    key,
                    {
                        "status": "MANUAL",
                        "candidate": {"kept": key},
                        "processing": {"status": "NONE"},
                    },
                )

            with mock.patch.object(store, "_save_unlocked", wraps=store._save_unlocked) as saver:
                changed = store.bind_release_hashes(
                    {"first": ("a" * 40,), "second": ("b" * 40,)}
                )

            self.assertEqual(changed, 2)
            saver.assert_called_once_with()
            self.assertEqual(store.get_release("first")["torrent_hashes"], ["a" * 40])
            self.assertEqual(store.get_release("second")["torrent_hashes"], ["b" * 40])
            self.assertEqual(store.get_release("first")["candidate"], {"kept": "first"})
            self.assertEqual(store.get_release("second")["processing"], {"status": "NONE"})

    def test_terminal_false_cannot_reopen_a_decided_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": module.STATE_VERSION,
                        "releases": {
                            "example": {
                                "status": "NOT_FOUND",
                                "terminal": False,
                            }
                        },
                        "rate_limits": {},
                        "queries": {},
                    }
                ),
                encoding="utf-8",
            )

            store = module.StateStore(path)
            self.assertTrue(store.is_terminal("example"))
            self.assertTrue(store.get_release("example")["terminal"])

    def test_unknown_release_status_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": module.STATE_VERSION,
                        "releases": {
                            "example": {
                                "status": "SEARCH_ME_AGAIN",
                                "terminal": False,
                            }
                        },
                        "rate_limits": {},
                        "queries": {},
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(module.StateFormatError):
                module.StateStore(path)

            store = module.StateStore(Path(directory) / "fresh.json")
            with self.assertRaises(module.StateFormatError):
                store.record_release(
                    "example",
                    {"status": "SEARCH_ME_AGAIN", "terminal": False},
                )

    def test_review_statuses_default_to_terminal_when_field_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = module.StateStore(path)
            for status in ("MANUAL", "NOT_MEDIA", "RECHECK_REQUIRED"):
                key = status.casefold()
                store.record_release(key, {"status": status})
                self.assertTrue(store.is_terminal(key))

    def test_legacy_list_becomes_versioned_terminal_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text(json.dumps(["release-one", "release-two"]), encoding="utf-8")

            store = module.StateStore(path)

            self.assertTrue(store.is_terminal("release-one"))
            records = store.terminal_records(status="legacy_seen")
            self.assertEqual(set(records), {"release-one", "release-two"})
            self.assertIsNone(records["release-one"]["candidate"])

            store.save()
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["version"], module.STATE_VERSION)
            self.assertIn("rate_limits", persisted)
            self.assertEqual(persisted["queries"], {})
            self.assertIsInstance(persisted["releases"], dict)

    def test_v1_state_is_atomically_migrated_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            original_release = {
                "status": "NOT_FOUND",
                "terminal": True,
                "candidate": {"name": "Example.2026.1080p.WEB-DL"},
                "decision_version": 7,
                "custom_fact": {"kept": [1, 2, 3]},
            }
            original_rate_limits = {
                "aither": {
                    "attempts_total": 126,
                    "attempts_since_pause": 6,
                    "next_allowed_at": 1234.5,
                }
            }
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "releases": {"example": original_release},
                        "rate_limits": original_rate_limits,
                        "unknown_top_level": {"also": "kept"},
                    }
                ),
                encoding="utf-8",
            )

            store = module.StateStore(path)

            self.assertEqual(store.get_release("example"), original_release)
            self.assertEqual(store.rate_snapshot(), original_rate_limits["aither"])
            self.assertIsNone(store.get_query("missing"))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["version"], 2)
            self.assertEqual(persisted["releases"]["example"], original_release)
            self.assertEqual(persisted["rate_limits"], original_rate_limits)
            self.assertEqual(persisted["queries"], {})
            self.assertEqual(
                persisted["unknown_top_level"],
                {"also": "kept"},
            )

    def test_query_key_shares_episode_and_technical_variants(self) -> None:
        first = module.build_query_key(
            {"tmdb_id": 123, "kind": "tv"},
            "TV",
            "S02",
            episode=1,
            release_type="WEB-DL",
            resolution="1080p",
        )
        second = module.build_query_key(
            {"kind": "TV", "tmdb_id": 123},
            "tv",
            2,
            episode=9,
            release_type="Full Disc",
            resolution="2160p",
        )

        self.assertEqual(first, second)
        self.assertNotEqual(
            first,
            module.build_query_key({"tmdb_id": 123, "kind": "tv"}, "tv", 3),
        )
        self.assertNotEqual(
            first,
            module.build_query_key({"tmdb_id": 124, "kind": "tv"}, "tv", 2),
        )

    def test_sanitized_query_cache_is_isolated_and_persists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            key = module.build_query_key("tmdb:929", "movie")
            caller_record = {
                "identity": {"tmdb_id": 929},
                "data": [
                    {
                        "name": "Godzilla.1998.CEE.COMPLETE.BLURAY",
                        "size": 46_381_579_621,
                        "download_link": "must-not-be-persisted",
                    }
                ],
                "has_next": False,
                "queried_at": "2026-08-11T00:00:00Z",
                "token": "must-not-be-persisted",
            }

            stored = module.StateStore(path).record_query(key, caller_record)
            self.assertNotIn("token", stored)
            self.assertNotIn("download_link", stored["data"][0])
            self.assertFalse(stored["has_next"])
            self.assertEqual(stored["queried_at"], "2026-08-11T00:00:00Z")

            caller_record["identity"]["tmdb_id"] = 1
            stored["identity"]["tmdb_id"] = 2
            reloaded = module.StateStore(path).get_query(key)
            assert reloaded is not None
            self.assertEqual(reloaded["identity"]["tmdb_id"], 929)
            self.assertNotIn("token", reloaded)
            self.assertNotIn("download_link", reloaded["data"][0])

            reloaded["identity"]["tmdb_id"] = 3
            self.assertEqual(
                module.StateStore(path).get_query(key)["identity"]["tmdb_id"],
                929,
            )

    def test_terminal_candidate_and_processing_are_saved_together(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            candidate = {
                "name": "Example.2026.1080p-GROUP",
                "torrent_hash": "abc123",
                "content_path": "/srv/qb/Example.2026.1080p-GROUP",
            }
            store = module.StateStore(path)
            store.record_release(
                "example-key",
                {
                    "status": "missing",
                    "candidate": candidate,
                    "processing": {"status": "PENDING"},
                },
            )

            reloaded = module.StateStore(path)
            self.assertTrue(reloaded.is_terminal("example-key"))
            record = reloaded.terminal_records("MISSING")["example-key"]
            self.assertEqual(record["candidate"], candidate)

            reloaded.update_processing(
                "example-key",
                status="PREPARED_SEPARATE",
                output_paths=["/srv/qb/up/Example.mkv"],
            )
            final_record = module.StateStore(path).get_release("example-key")
            assert final_record is not None
            self.assertEqual(final_record["candidate"], candidate)
            self.assertEqual(
                final_record["processing"]["status"],
                "PREPARED_SEPARATE",
            )

    def test_failed_replace_leaves_previous_disk_and_memory_state_intact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = module.StateStore(path)
            store.record_release("first", {"status": "MISSING", "candidate": {}})

            with mock.patch.object(
                module.os,
                "replace",
                side_effect=OSError("simulated replace failure"),
            ):
                with self.assertRaises(module.StateError):
                    store.record_release(
                        "second",
                        {"status": "MISSING", "candidate": {}},
                    )

            self.assertIsNone(store.get_release("second"))
            reloaded = module.StateStore(path)
            self.assertTrue(reloaded.is_terminal("first"))
            self.assertIsNone(reloaded.get_release("second"))
            self.assertEqual(list(Path(directory).glob(".*.tmp")), [])

    def test_corrupt_or_unknown_state_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(module.StateFormatError):
                module.StateStore(path)

            path.write_text(
                json.dumps({"version": 999, "releases": {}, "rate_limits": {}}),
                encoding="utf-8",
            )
            with self.assertRaises(module.StateFormatError):
                module.StateStore(path)

    @unittest.skipUnless(os.name == "posix", "POSIX file modes are required")
    def test_state_file_mode_is_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            module.StateStore(path).save()
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class AitherRateGateTests(unittest.TestCase):
    def test_normal_delay_and_tenth_attempt_pause_persist_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            clock = FakeClock()
            first_gate = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )

            for _ in range(9):
                self.assertEqual(first_gate.call(lambda: "ok"), "ok")

            second_gate = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )
            second_gate.call(lambda: "tenth")

            self.assertEqual(clock.sleeps, [5.0] * 9 + [30.0])
            snapshot = module.StateStore(path).rate_snapshot()
            self.assertEqual(snapshot["attempts_total"], 10)
            self.assertEqual(snapshot["attempts_since_pause"], 0)
            self.assertIsNone(snapshot["in_flight"])

    def test_http_error_and_429_response_both_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            clock = FakeClock()
            gate = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )

            def fail() -> None:
                raise OSError("network error without secret data")

            with self.assertRaises(OSError):
                gate.call(fail)
            response = gate.call(lambda: SimpleNamespace(status_code=429))

            self.assertEqual(response.status_code, 429)
            self.assertEqual(gate.attempts_total, 2)
            self.assertEqual(clock.sleeps, [5.0, 5.0])

    def test_interrupted_post_pause_cannot_be_skipped_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            clock = FakeClock()
            gate = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )
            for _ in range(9):
                gate.call(lambda: None)

            interrupted_sleeps: list[float] = []

            def interrupt_sleep(seconds: float) -> None:
                interrupted_sleeps.append(seconds)
                raise KeyboardInterrupt

            gate.sleeper = interrupt_sleep
            with self.assertRaises(KeyboardInterrupt):
                gate.call(lambda: None)
            self.assertEqual(interrupted_sleeps, [30.0])

            restarted = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )
            restarted.call(lambda: None)
            self.assertEqual(clock.sleeps[-2:], [30.0, 5.0])
            self.assertEqual(restarted.attempts_total, 11)

    def test_crash_during_tenth_request_requires_full_pause_on_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            clock = FakeClock()
            gate = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )
            for _ in range(9):
                gate.call(lambda: None)

            reservation = gate._begin_attempt()  # Simulate process death in HTTP code.
            self.assertEqual(reservation.number, 10)
            self.assertEqual(reservation.post_delay, 30.0)
            clock.now += 3_600.0  # Even a stale in-flight attempt gets a fresh pause.

            restarted = module.AitherRateGate(
                module.StateStore(path), clock=clock, sleeper=clock.sleep
            )
            restarted.call(lambda: None)

            self.assertEqual(clock.sleeps[-2:], [30.0, 5.0])
            self.assertEqual(restarted.attempts_total, 11)


@unittest.skipUnless(
    os.name == "posix" and module.fcntl is not None and hasattr(os, "O_NOFOLLOW"),
    "POSIX fcntl/O_NOFOLLOW is required",
)
class SingleProcessLockTests(unittest.TestCase):
    def test_lock_is_0600_non_reentrant_and_rejects_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock_path = Path(directory) / "aither.lock"
            first = module.SingleProcessLock(lock_path)
            second = module.SingleProcessLock(lock_path)
            self.assertTrue(first.acquire())
            try:
                self.assertEqual(stat.S_IMODE(lock_path.stat().st_mode), 0o600)
                self.assertFalse(second.acquire())
            finally:
                first.release()

            self.assertTrue(second.acquire())
            second.release()

            symlink_path = Path(directory) / "symlink.lock"
            symlink_path.symlink_to(lock_path)
            with self.assertRaises(OSError):
                module.SingleProcessLock(symlink_path).acquire()


if __name__ == "__main__":
    unittest.main()
