from __future__ import annotations

import os
import copy
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from media_prepare import (  # noqa: E402
    _LiveOwnerValidator,
    _hydrate_candidates_from_qbit,
    _validate_plain_ready_records,
    ArchiveError,
    ArchiveMember,
    Candidate,
    Decision,
    MediaPreparer,
    OwnershipError,
    ProcessingResult,
    ProcessingStatus,
    QbitApi,
    ReviewBatch,
    TransactionError,
    UnsafePathError,
    apply_processing_result,
    apply_review_decisions,
    collect_decisions,
    confirm_review_batch,
    detect_media,
    execute_review_batch,
    load_candidates_from_state,
    render_ready_txt,
    review_and_process,
    safe_relative_path,
    upsert_candidate_record,
)


class MockExtractor:
    def __init__(self, payloads):
        self.payloads = payloads
        self.inspected = []
        self.extracted = []

    def inspect(self, source: Path):
        self.inspected.append(source)
        try:
            payload = self.payloads[source.name]
        except KeyError as error:
            raise ArchiveError(f"unexpected source: {source.name}") from error
        return tuple(
            ArchiveMember(Path(name), False, len(content))
            for name, content in payload.items()
        )

    def extract(self, source: Path, destination: Path):
        self.extracted.append((source, destination))
        for name, content in self.payloads[source.name].items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


class MockQbit:
    def __init__(self, fail_remove=False):
        self.calls = []
        self.fail_remove = fail_remove

    def stop(self, hashes):
        self.calls.append(("stop", tuple(hashes)))

    def start(self, hashes):
        self.calls.append(("start", tuple(hashes)))

    def remove_list_only(self, hashes):
        self.calls.append(("remove_list_only", tuple(hashes)))
        if self.fail_remove:
            raise TransactionError("mock qB failure")


class RecordingStore:
    def __init__(self):
        self.updates = []

    def update_processing(self, key, processing=None, **changes):
        merged = dict(processing or {})
        merged.update(changes)
        self.updates.append((key, merged))
        return merged


class StatefulStore:
    def __init__(self, state):
        self.state = copy.deepcopy(state)
        self.updates = []

    def snapshot(self):
        return copy.deepcopy(self.state)

    def update_processing(self, key, processing=None, **changes):
        merged = dict(processing or {})
        merged.update(changes)
        current = self.state["releases"][key].setdefault("processing", {})
        current.update(copy.deepcopy(merged))
        self.updates.append((key, copy.deepcopy(merged)))
        return copy.deepcopy(self.state["releases"][key])


class HighLevelQbit(MockQbit):
    def __init__(self, torrents, files):
        super().__init__()
        self._torrents = copy.deepcopy(torrents)
        self._files = copy.deepcopy(files)
        self.file_calls = []

    def torrents(self, filter_name="all", hashes=None):
        self.asserted_filter = filter_name
        if hashes:
            wanted = set(hashes)
            return [item for item in self._torrents if item["hash"] in wanted]
        return copy.deepcopy(self._torrents)

    def files(self, torrent_hash):
        self.file_calls.append(torrent_hash)
        return copy.deepcopy(self._files[torrent_hash])


def make_candidate(save: Path, *, name="Release", hashes=("abc",)):
    root = save / name
    root.mkdir(parents=True)
    (root / "release.part1.rar").write_bytes(b"rar-1")
    (root / "release.part2.rar").write_bytes(b"rar-2")
    (root / "release.nfo").write_text("metadata", encoding="utf-8")
    return Candidate(
        key=name.casefold(),
        name=name,
        hashes=hashes,
        save_path=save,
        content_path=root,
        file_names=(
            f"{name}/release.part1.rar",
            f"{name}/release.part2.rar",
            f"{name}/release.nfo",
        ),
        running_hashes=hashes,
    )


class PathAndDetectionTests(unittest.TestCase):
    def test_safe_relative_path_rejects_traversal_and_absolute_forms(self):
        for value in (
            "../escape.mkv",
            "folder/../../escape.mkv",
            "/absolute.mkv",
            r"C:\absolute.mkv",
            r"\\server\share\file.mkv",
            "bad\x00name.mkv",
            "line\nfeed.mkv",
        ):
            with self.subTest(value=value):
                with self.assertRaises(UnsafePathError):
                    safe_relative_path(value)
        self.assertEqual(safe_relative_path(r"Release\movie.mkv"), Path("Release/movie.mkv"))

    def test_detects_modern_old_rar_and_iso(self):
        detected = detect_media(
            [
                "A/a.part01.rar",
                "A/a.part02.rar",
                "B/b.rar",
                "B/b.r00",
                "B/b.r01",
                "C/disc.iso",
                "C/info.nfo",
            ]
        )
        self.assertEqual(len(detected.rar_sets), 2)
        self.assertEqual(detected.rar_sets[0].volumes, (Path("A/a.part01.rar"), Path("A/a.part02.rar")))
        self.assertEqual(detected.iso_files, (Path("C/disc.iso"),))
        self.assertEqual(detected.direct_files, (Path("C/info.nfo"),))

    def test_rejects_missing_or_orphan_rar_volumes(self):
        with self.assertRaises(ArchiveError):
            detect_media(["A/a.part01.rar", "A/a.part03.rar"])
        with self.assertRaises(ArchiveError):
            detect_media(["A/a.r00"])

    def test_archive_member_rejects_traversal(self):
        with self.assertRaises(UnsafePathError):
            ArchiveMember(Path("../outside"), False, 1)


class StateAndReviewTests(unittest.TestCase):
    def setUp(self):
        self.candidate = Candidate(
            key="release",
            name="Release",
            hashes=("abc",),
            save_path=Path("/save"),
            content_path=Path("/save/Release"),
            file_names=("Release/file.iso",),
        )

    def test_candidate_and_results_use_caller_state_only(self):
        original = {"version": 1, "releases": {}, "rate_limits": {}}
        with_candidate = upsert_candidate_record(original, self.candidate)
        self.assertEqual(original["releases"], {})
        loaded = load_candidates_from_state(with_candidate)
        self.assertEqual(loaded, (self.candidate,))

        reviewed = apply_review_decisions(
            with_candidate,
            ReviewBatch(((self.candidate.key, Decision.SEPARATE),)),
        )
        ready = apply_processing_result(
            reviewed,
            ProcessingResult(
                self.candidate.key,
                ProcessingStatus.READY,
                "separate",
                Path("/up/Release"),
            ),
        )
        self.assertEqual(
            ready["releases"]["release"]["candidate"],
            with_candidate["releases"]["release"]["candidate"],
        )
        self.assertEqual(render_ready_txt(ready), f"{Path('/up/Release')}\n")

    def test_collect_decisions_is_a_separate_complete_phase(self):
        second = Candidate(
            key="second",
            name="Second",
            hashes=("def",),
            save_path=Path("/save"),
            content_path=Path("/save/Second"),
            file_names=("Second/file.iso",),
        )
        answers = iter(["bad", "i", "k"])
        output = []
        batch = collect_decisions(
            [self.candidate, second],
            input_fn=lambda _prompt: next(answers),
            output_fn=output.append,
        )
        self.assertFalse(batch.cancelled)
        self.assertEqual(
            batch.decisions,
            (("release", Decision.YES), ("second", Decision.SEPARATE)),
        )
        self.assertTrue(any("Érvénytelen" in line for line in output))

    def test_final_confirmation_shows_complete_plan(self):
        output = []
        confirmed = confirm_review_batch(
            [self.candidate],
            ReviewBatch(((self.candidate.key, Decision.YES),)),
            input_fn=lambda _prompt: "igen",
            output_fn=output.append,
        )
        self.assertTrue(confirmed)
        self.assertTrue(any("Release" in line and "yes" in line for line in output))

    def test_plain_ready_paths_plural_is_in_derived_list(self):
        state = {
            "version": 1,
            "releases": {
                "plain": {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {"content_path": "/data/plain.mkv"},
                    "processing": {
                        "status": "READY",
                        "ready_paths": ["/data/plain.mkv"],
                    },
                }
            },
            "rate_limits": {},
        }
        self.assertEqual(render_ready_txt(state), f"{Path('/data/plain.mkv')}\n")

    def test_quit_cancels_entire_batch_and_execution(self):
        state = upsert_candidate_record(
            {"version": 1, "releases": {}, "rate_limits": {}}, self.candidate
        )
        answers = iter(["i", "q"])
        second = Candidate(
            key="second",
            name="Second",
            hashes=("def",),
            save_path=Path("/save"),
            content_path=Path("/save/Second"),
            file_names=("Second/file.iso",),
        )
        batch = collect_decisions(
            [self.candidate, second],
            input_fn=lambda _prompt: next(answers),
            output_fn=lambda _message: None,
        )
        self.assertTrue(batch.cancelled)

        class MustNotRun:
            def prepare(self, *_args):
                raise AssertionError("phase two ran after cancellation")

        unchanged, results = execute_review_batch(
            state, [self.candidate, second], batch, MustNotRun()
        )
        self.assertEqual(results, ())
        self.assertEqual(unchanged, state)


class TransactionTests(unittest.TestCase):
    def _preparer(self, save, qbit, rar, iso=None, owner=None):
        return MediaPreparer(
            source_roots=[save],
            up_root=save / "up",
            qbit=qbit,
            owner_validator=owner or (lambda candidate, _paths: candidate.hashes),
            rar_extractor=rar,
            iso_extractor=iso,
            min_free_bytes=0,
        )

    def test_in_place_replaces_entire_release_and_removes_qbit_list_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar)

            result = preparer.prepare(candidate, Decision.YES)

            release = save / "Release"
            self.assertEqual(result.ready_path, release)
            self.assertEqual(
                sorted(str(path.relative_to(release)) for path in release.rglob("*")),
                ["movie.mkv"],
            )
            self.assertEqual((release / "movie.mkv").read_bytes(), b"payload")
            self.assertEqual(
                qbit.calls,
                [("stop", ("abc",)), ("remove_list_only", ("abc",))],
            )
            self.assertFalse(any("quarantine" in path.name for path in save.iterdir()))

    def test_in_place_rolls_back_when_qbit_remove_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = MockQbit(fail_remove=True)
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar)

            with self.assertRaises(TransactionError):
                preparer.prepare(candidate, Decision.YES)

            release = save / "Release"
            self.assertTrue((release / "release.part1.rar").is_file())
            self.assertTrue((release / "release.nfo").is_file())
            self.assertFalse((release / "movie.mkv").exists())
            self.assertEqual(qbit.calls[-1], ("start", ("abc",)))

    def test_partial_qbit_stop_failure_restarts_previously_running_hashes(self):
        class FailStopQbit(MockQbit):
            def stop(self, hashes):
                super().stop(hashes)
                raise TransactionError("stop confirmation timed out")

        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = FailStopQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar)

            with self.assertRaises(TransactionError):
                preparer.prepare(candidate, Decision.YES)

            self.assertTrue((save / "Release/release.part1.rar").is_file())
            self.assertEqual(
                qbit.calls,
                [("stop", ("abc",)), ("start", ("abc",))],
            )

    def test_separate_publishes_under_up_and_does_not_touch_qbit_or_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar)

            result = preparer.prepare(candidate, Decision.SEPARATE)

            destination = save / "up" / "Release"
            self.assertEqual(result.ready_path, destination)
            self.assertEqual((destination / "movie.mkv").read_bytes(), b"payload")
            self.assertTrue((save / "Release" / "release.part1.rar").exists())
            self.assertTrue((save / "Release" / "release.nfo").exists())
            self.assertEqual(qbit.calls, [])

    def test_nested_iso_from_rar_is_expanded_and_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"disc.iso": b"image"}})
            iso = MockExtractor(
                {
                    "disc.iso": {
                        "BDMV/index.bdmv": b"index",
                        "BDMV/STREAM/00001.m2ts": b"video",
                    }
                }
            )
            preparer = self._preparer(save, qbit, rar, iso)

            result = preparer.prepare(candidate, Decision.SEPARATE)

            self.assertTrue((result.ready_path / "BDMV/index.bdmv").is_file())
            self.assertFalse(any(result.ready_path.rglob("*.iso")))

    def test_owner_mismatch_blocks_destructive_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(
                save,
                qbit,
                rar,
                owner=lambda _candidate, _paths: ("abc", "overlapping-hash"),
            )
            with self.assertRaises(OwnershipError):
                preparer.prepare(candidate, Decision.YES)
            self.assertTrue((save / "Release" / "release.part1.rar").exists())
            self.assertEqual(qbit.calls, [])

    def test_other_qbit_file_inside_release_blocks_yes_but_not_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            unrelated = save / "Release/unrelated.mkv"
            unrelated.write_bytes(b"other torrent")

            def owners(_candidate, affected_paths):
                names = {path.name for path in affected_paths}
                return ("abc", "other-hash") if "unrelated.mkv" in names else ("abc",)

            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar, owner=owners)
            with self.assertRaises(OwnershipError):
                preparer.prepare(candidate, Decision.YES)
            self.assertTrue(unrelated.is_file())
            self.assertEqual(qbit.calls, [])

            result = preparer.prepare(candidate, Decision.SEPARATE)
            self.assertEqual(
                (result.ready_path / "movie.mkv").read_bytes(), b"payload"
            )
            self.assertTrue(unrelated.is_file())
            self.assertEqual(qbit.calls, [])

    def test_new_overlapping_torrent_between_validations_blocks_commit(self):
        class ChangingQbit(MockQbit):
            def __init__(self, first, second, files):
                super().__init__()
                self.snapshots = [first, second]
                self.files_by_hash = files
                self.torrent_calls = 0
                self.file_calls = []

            def torrents(self, filter_name="all", hashes=None):
                del filter_name, hashes
                index = min(self.torrent_calls, len(self.snapshots) - 1)
                self.torrent_calls += 1
                return copy.deepcopy(self.snapshots[index])

            def files(self, torrent_hash):
                self.file_calls.append(torrent_hash)
                return copy.deepcopy(self.files_by_hash[torrent_hash])

        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            unrelated = save / "Release/unrelated.mkv"
            unrelated.write_bytes(b"other")
            original = {
                "hash": "abc",
                "name": "Release",
                "save_path": str(save),
                "content_path": str(save / "Release"),
            }
            overlapping = {
                "hash": "other-hash",
                "name": "overlap",
                "save_path": str(save),
                "content_path": str(unrelated),
            }
            qbit = ChangingQbit(
                [original],
                [original, overlapping],
                {
                    "abc": [{"name": name} for name in candidate.file_names],
                    "other-hash": [{"name": "Release/unrelated.mkv"}],
                },
            )
            validator = _LiveOwnerValidator(qbit, [original])
            preparer = MediaPreparer(
                source_roots=[save],
                up_root=save / "up",
                qbit=qbit,
                owner_validator=validator,
                rar_extractor=MockExtractor(
                    {"release.part1.rar": {"movie.mkv": b"payload"}}
                ),
                min_free_bytes=0,
            )

            with self.assertRaises(OwnershipError):
                preparer.prepare(candidate, Decision.YES)

            self.assertGreaterEqual(qbit.torrent_calls, 2)
            self.assertIn("other-hash", qbit.file_calls)
            self.assertEqual(qbit.calls, [])
            self.assertTrue((save / "Release/release.part1.rar").exists())
            self.assertTrue(unrelated.exists())

    def test_expected_hash_manifest_outside_release_blocks_yes_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            outside = save / "Elsewhere/extra.mkv"
            outside.parent.mkdir()
            outside.write_bytes(b"outside")
            torrent = {
                "hash": "abc",
                "name": "grouped-owner",
                "state": "uploading",
                "save_path": str(save),
                "content_path": str(save / "Release"),
            }
            files = {
                "abc": [
                    *({"name": name} for name in candidate.file_names),
                    {"name": "Elsewhere/extra.mkv"},
                ]
            }
            qbit = HighLevelQbit([torrent], files)
            validator = _LiveOwnerValidator(qbit, [torrent])
            preparer = MediaPreparer(
                source_roots=[save],
                up_root=save / "up",
                qbit=qbit,
                owner_validator=validator,
                rar_extractor=MockExtractor(
                    {"release.part1.rar": {"movie.mkv": b"payload"}}
                ),
                min_free_bytes=0,
            )

            with self.assertRaises(OwnershipError):
                preparer.prepare(candidate, Decision.YES)
            self.assertTrue(outside.is_file())
            self.assertEqual(qbit.calls, [])

            result = preparer.prepare(candidate, Decision.SEPARATE)
            self.assertTrue((result.ready_path / "movie.mkv").is_file())
            self.assertTrue(outside.is_file())
            self.assertEqual(qbit.calls, [])

    def test_live_validator_refreshes_manifest_inside_affected_content_root(self):
        class RenamedFileQbit:
            def __init__(self, torrent):
                self.torrent = torrent
                self.file_calls = 0

            def torrents(self, filter_name="all", hashes=None):
                del filter_name, hashes
                return [copy.deepcopy(self.torrent)]

            def files(self, torrent_hash):
                self.file_calls += 1
                name = (
                    "Release/old-name.mkv"
                    if self.file_calls == 1
                    else "Release/new-name.mkv"
                )
                return [{"name": name}]

        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            release = save / "Release"
            release.mkdir()
            old = release / "old-name.mkv"
            new = release / "new-name.mkv"
            old.write_bytes(b"old")
            new.write_bytes(b"new")
            torrent = {
                "hash": "abc",
                "name": "Release",
                "save_path": str(save),
                "content_path": str(release),
            }
            qbit = RenamedFileQbit(torrent)
            validator = _LiveOwnerValidator(qbit, [torrent])
            validator.refresh()

            owners = tuple(validator(None, (new,)))

            self.assertEqual(owners, ("abc",))
            self.assertEqual(qbit.file_calls, 2)

    def test_flat_single_iso_gets_safe_container_not_broad_root_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            image = save / "disc.iso"
            image.write_bytes(b"image")
            unrelated = save / "unrelated.txt"
            unrelated.write_text("keep", encoding="utf-8")
            candidate = Candidate(
                key="disc",
                name="disc.iso",
                hashes=("iso-hash",),
                save_path=save,
                content_path=image,
                file_names=("disc.iso",),
                running_hashes=("iso-hash",),
            )
            qbit = MockQbit()
            iso = MockExtractor({"disc.iso": {"BDMV/index.bdmv": b"index"}})
            preparer = self._preparer(save, qbit, MockExtractor({}), iso)

            result = preparer.prepare(candidate, Decision.YES)

            self.assertEqual(result.ready_path, save / "disc")
            self.assertTrue((save / "disc/BDMV/index.bdmv").is_file())
            self.assertFalse(image.exists())
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")

    def test_source_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            outside = save / "outside"
            outside.write_text("outside", encoding="utf-8")
            link = save / "Release" / "link"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            qbit = MockQbit()
            rar = MockExtractor({"release.part1.rar": {"movie.mkv": b"payload"}})
            preparer = self._preparer(save, qbit, rar)
            with self.assertRaises(UnsafePathError):
                preparer.prepare(candidate, Decision.YES)
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_extractor_symlink_output_is_rejected_before_publish(self):
        class SymlinkExtractor:
            def inspect(self, _source):
                return (ArchiveMember(Path("movie.mkv"), False, 1),)

            def extract(self, _source, destination):
                os.symlink(destination.parent, destination / "movie.mkv")

        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            try:
                probe = save / "probe-link"
                os.symlink(save, probe)
                probe.unlink()
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")
            preparer = self._preparer(save, MockQbit(), SymlinkExtractor())
            with self.assertRaises(ArchiveError):
                preparer.prepare(candidate, Decision.SEPARATE)
            self.assertTrue((save / "Release/release.part1.rar").exists())
            self.assertFalse((save / "up/Release").exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_extractor_special_file_output_is_rejected_before_publish(self):
        class FifoExtractor:
            def inspect(self, _source):
                return (ArchiveMember(Path("movie.mkv"), False, 0),)

            def extract(self, _source, destination):
                os.mkfifo(destination / "movie.mkv")

        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            preparer = self._preparer(save, MockQbit(), FifoExtractor())
            try:
                with self.assertRaises(ArchiveError):
                    preparer.prepare(candidate, Decision.SEPARATE)
            except (OSError, NotImplementedError):
                self.skipTest("FIFO creation is unavailable on this platform")
            self.assertTrue((save / "Release/release.part1.rar").exists())


class AdapterAndOrchestrationTests(unittest.TestCase):
    def test_qbit_delete_files_false_is_not_caller_configurable(self):
        class Response:
            status_code = 200

        class Session:
            def __init__(self):
                self.calls = []

            def post(self, url, data, timeout):
                self.calls.append((url, data, timeout))
                return Response()

        session = Session()
        client = QbitApi(session, "http://127.0.0.1:1234")
        client.remove_list_only(("abc", "def"))
        self.assertEqual(
            session.calls[0][1],
            {"hashes": "abc|def", "deleteFiles": "false"},
        )
        with self.assertRaises(TypeError):
            client.remove_list_only(("abc",), True)  # type: ignore[arg-type]

    def test_execute_batch_updates_supplied_state_store(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            candidate = make_candidate(save)
            state = upsert_candidate_record(
                {"version": 1, "releases": {}, "rate_limits": {}}, candidate
            )
            preparer = MediaPreparer(
                source_roots=[save],
                up_root=save / "up",
                qbit=MockQbit(),
                owner_validator=lambda item, _paths: item.hashes,
                rar_extractor=MockExtractor(
                    {"release.part1.rar": {"movie.mkv": b"payload"}}
                ),
                min_free_bytes=0,
            )
            store = RecordingStore()
            updated, results = execute_review_batch(
                state,
                [candidate],
                ReviewBatch(((candidate.key, Decision.SEPARATE),)),
                preparer,
                state_store=store,
            )
            self.assertEqual(results[0].status, ProcessingStatus.READY)
            self.assertEqual(
                updated["releases"][candidate.key]["processing"]["status"], "READY"
            )
            self.assertEqual(store.updates[-1][1]["status"], "READY")

    def _high_level_fixture(self, save):
        candidate = make_candidate(save)
        plain = save / "plain.mkv"
        plain.write_bytes(b"plain")
        state = {
            "version": 1,
            "releases": {
                candidate.key: {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {
                        "key": candidate.key,
                        "name": candidate.name,
                        "hashes": ["abc"],
                        "save_path": str(save),
                        "content_path": str(save / "Release"),
                        "processing": {"status": "PENDING", "ready_paths": []},
                    },
                    "processing": {"status": "PENDING", "ready_paths": []},
                },
                "plain": {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {
                        "key": "plain",
                        "name": "plain.mkv",
                        "hashes": ["plain-hash"],
                        "save_path": str(save),
                        "content_path": str(plain),
                        "processing": {
                            "status": "READY",
                            "ready_paths": [str(plain)],
                            "mode": "plain",
                        },
                    },
                    "processing": {
                        "status": "READY",
                        "ready_paths": [str(plain)],
                        "mode": "plain",
                    },
                },
            },
            "rate_limits": {},
        }
        torrents = [
            {
                "hash": "abc",
                "name": "Release",
                "state": "uploading",
                "save_path": str(save),
                "content_path": str(save / "Release"),
            },
            {
                "hash": "plain-hash",
                "name": "plain.mkv",
                "state": "uploading",
                "save_path": str(save),
                "content_path": str(plain),
            },
        ]
        files = {
            "abc": [
                {"name": "Release/release.part1.rar"},
                {"name": "Release/release.part2.rar"},
                {"name": "Release/release.nfo"},
            ],
            "plain-hash": [{"name": "plain.mkv"}],
        }
        return StatefulStore(state), HighLevelQbit(torrents, files), plain

    def test_high_level_reviews_confirms_indexes_owners_and_regenerates_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            store, qbit, plain = self._high_level_fixture(save)
            answers = iter(["i", "igen"])
            ready_file = save / "ready.txt"
            summary = review_and_process(
                store,
                qbit,
                [save],
                save / "up",
                ready_file,
                input_func=lambda _prompt: next(answers),
                output_func=lambda _message: None,
                rar_extractor=MockExtractor(
                    {"release.part1.rar": {"movie.mkv": b"payload"}}
                ),
                min_free_bytes=0,
            )
            self.assertFalse(summary.cancelled)
            self.assertEqual(summary.ready, 1)
            self.assertEqual(qbit.calls[:2], [
                ("stop", ("abc",)),
                ("remove_list_only", ("abc",)),
            ])
            self.assertIn("plain-hash", qbit.file_calls)
            ready_lines = set(ready_file.read_text(encoding="utf-8").splitlines())
            self.assertEqual(
                ready_lines,
                {str(plain), str(save / "Release")},
            )

    def test_high_level_final_no_changes_nothing_but_still_rebuilds_ready_txt(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            store, qbit, plain = self._high_level_fixture(save)
            answers = iter(["i", "nem"])
            ready_file = save / "ready.txt"
            summary = review_and_process(
                store,
                qbit,
                [save],
                save / "up",
                ready_file,
                input_func=lambda _prompt: next(answers),
                output_func=lambda _message: None,
                rar_extractor=MockExtractor(
                    {"release.part1.rar": {"movie.mkv": b"payload"}}
                ),
                min_free_bytes=0,
            )
            self.assertTrue(summary.cancelled)
            self.assertEqual(qbit.calls, [])
            self.assertTrue((save / "Release/release.part1.rar").exists())
            self.assertEqual(
                ready_file.read_text(encoding="utf-8"), f"{plain}\n"
            )

    def test_hydration_overwrites_stale_running_hashes_from_live_qbit_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            save = Path(temporary)
            store, qbit, _plain = self._high_level_fixture(save)
            record = store.state["releases"]["release"]["candidate"]
            record["running_hashes"] = ["abc"]
            qbit._torrents[0]["state"] = "stoppedUP"
            stopped = _hydrate_candidates_from_qbit(
                store.snapshot(), qbit, qbit.torrents()
            )
            self.assertEqual(stopped[0].running_hashes, ())

            record["running_hashes"] = []
            qbit._torrents[0]["state"] = "queuedUP"
            queued = _hydrate_candidates_from_qbit(
                store.snapshot(), qbit, qbit.torrents()
            )
            self.assertEqual(queued[0].running_hashes, ("abc",))

    @staticmethod
    def _plain_ready_state(root, content_path):
        return {
            "version": 1,
            "releases": {
                "plain": {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {
                        "key": "plain",
                        "name": "plain.mkv",
                        "hashes": ["plain-hash"],
                        "save_path": str(root),
                        "content_path": str(content_path),
                        "file_names": ["plain.mkv"],
                    },
                    "processing": {
                        "status": "READY",
                        "mode": "plain",
                        "ready_path": str(content_path),
                    },
                }
            },
            "rate_limits": {},
        }

    def test_plain_ready_uses_current_live_qbit_content_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_path = root / "old.mkv"
            current_path = root / "moved.mkv"
            current_path.write_bytes(b"payload")
            store = StatefulStore(self._plain_ready_state(root, old_path))
            qbit = HighLevelQbit(
                [
                    {
                        "hash": "plain-hash",
                        "name": "moved.mkv",
                        "state": "uploading",
                        "save_path": str(root),
                        "content_path": str(current_path),
                    }
                ],
                {"plain-hash": [{"name": "moved.mkv"}]},
            )
            ready_file = root / "ready.txt"

            summary = review_and_process(
                store,
                qbit,
                [root],
                root / "up",
                ready_file,
                input_func=lambda _prompt: self.fail("plain READY prompted"),
                output_func=lambda _message: None,
                min_free_bytes=0,
            )

            self.assertEqual(summary.reviewed, 0)
            self.assertEqual(ready_file.read_text(encoding="utf-8"), f"{current_path}\n")
            processing = store.state["releases"]["plain"]["processing"]
            self.assertEqual(processing["status"], "READY")
            self.assertEqual(processing["ready_path"], str(current_path))

    def test_removed_plain_qbit_entry_is_retired_and_future_runs_do_not_crash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            content = root / "plain.mkv"
            content.write_bytes(b"payload")
            store = StatefulStore(self._plain_ready_state(root, content))
            qbit = HighLevelQbit([], {})
            ready_file = root / "ready.txt"

            for _ in range(2):
                summary = review_and_process(
                    store,
                    qbit,
                    [root],
                    root / "up",
                    ready_file,
                    input_func=lambda _prompt: self.fail("stale plain prompted"),
                    output_func=lambda _message: None,
                    min_free_bytes=0,
                )
                self.assertEqual(summary.reviewed, 0)

            processing = store.state["releases"]["plain"]["processing"]
            self.assertEqual(processing["status"], "STALE")
            self.assertEqual(processing["ready_path"], "")
            self.assertEqual(ready_file.read_text(encoding="utf-8"), "")

    def test_plain_ready_rejects_missing_outside_and_symlink_content_paths(self):
        with tempfile.TemporaryDirectory() as temporary, tempfile.TemporaryDirectory() as other:
            root = Path(temporary)
            outside = Path(other) / "outside.mkv"
            outside.write_bytes(b"outside")
            cases = [("missing", root / "missing.mkv"), ("outside", outside)]
            link = root / "link.mkv"
            try:
                os.symlink(outside, link)
            except (OSError, NotImplementedError):
                pass
            else:
                cases.append(("symlink", link))

            for label, content in cases:
                with self.subTest(label=label):
                    state = self._plain_ready_state(root, content)
                    qbit = HighLevelQbit(
                        [
                            {
                                "hash": "plain-hash",
                                "name": content.name,
                                "save_path": str(root),
                                "content_path": str(content),
                            }
                        ],
                        {},
                    )
                    updated = _validate_plain_ready_records(state, qbit, [root])
                    processing = updated["releases"]["plain"]["processing"]
                    self.assertEqual(processing["status"], "STALE")
                    self.assertEqual(processing["ready_path"], "")

    def test_processed_ready_modes_are_not_invalidated_when_qbit_is_absent(self):
        state = {
            "version": 1,
            "releases": {
                mode: {
                    "status": "NOT_FOUND",
                    "terminal": True,
                    "candidate": {"hashes": [f"{mode}-hash"]},
                    "processing": {
                        "status": "READY",
                        "mode": mode,
                        "ready_path": f"/prepared/{mode}",
                    },
                }
                for mode in ("yes", "separate")
            },
            "rate_limits": {},
        }

        updated = _validate_plain_ready_records(state, HighLevelQbit([], {}), [])

        self.assertEqual(
            render_ready_txt(updated),
            f"{Path('/prepared/separate')}\n{Path('/prepared/yes')}\n",
        )
        self.assertTrue(
            all(
                record["processing"]["status"] == "READY"
                for record in updated["releases"].values()
            )
        )


if __name__ == "__main__":
    unittest.main()
