"""End-to-end behaviour of the backup engine against real folders."""

import os
import shutil
import sys
import unittest

from diskbackup import rebuild, scanner
from diskbackup.executor import TMP_SUFFIX, Events, Executor, Stopped
from diskbackup.manifest import Manifest, ManifestError, index_path

from .helpers import DAY, SECOND, Workspace


def identity(path):
    """Enough to tell whether a file or folder on the 'HDD' was rewritten or merely renamed."""
    st = os.stat(path)
    return st.st_ino, st.st_mtime_ns, st.st_size


class StopAfter(Events):
    """Asks for a clean stop once `files` files have been copied."""

    def __init__(self, files):
        self.left = files

    def file_end(self, rel, outcome, size):
        self.left -= 1

    def should_stop(self):
        return self.left <= 0


class AbortMidFile(Events):
    def advance(self, count):
        raise Stopped()


class FirstBackup(Workspace):
    def test_copies_everything_and_records_it(self):
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")
        self.write("docs/deep/c.bin", b"\x00" * 5000)
        os.makedirs(self.s("empty/inner"))

        plan, result, manifest = self.backup()

        self.assertMirrored()
        self.assertTrue(os.path.isdir(self.cur("empty/inner")))
        self.assertEqual(result.copied_files, 3)
        self.assertEqual(plan.new_files, 3)
        self.assertEqual(set(manifest.files), {"a.txt", "docs/b.txt", "docs/deep/c.bin"})
        self.assertEqual(manifest.dirs, {"docs", "docs/deep", "empty", "empty/inner"})
        for rel, (size, mtime_ns, digest) in manifest.files.items():
            self.assertEqual(size, os.path.getsize(self.s(rel)))
            self.assertEqual(mtime_ns, os.stat(self.s(rel)).st_mtime_ns)
            self.assertEqual(digest, scanner.fingerprint(self.s(rel)))

    def test_modified_time_is_carried_over(self):
        self.write("a.txt", b"alpha", mtime_ns=1_600_000_000 * SECOND)
        self.backup()
        self.assertEqual(os.stat(self.cur("a.txt")).st_mtime_ns, 1_600_000_000 * SECOND)

    def test_no_temp_files_are_left(self):
        self.write("a.txt")
        self.write("b/c.txt")
        self.backup()
        self.assertFalse([name for name in self.tree(self.dst) if name.endswith(TMP_SUFFIX)])

    def test_odd_file_names(self):
        self.write("ünï çødé/日本語 file.txt", b"unicode")
        self.write("spaces and (brackets)/a & b.txt", b"punct")
        self.backup()
        self.assertMirrored()
        plan, _result, _manifest = self.backup()
        self.assertTrue(plan.is_empty)


class NothingChanged(Workspace):
    def test_second_run_does_nothing_at_all(self):
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")
        self.backup()
        before = {rel: identity(os.path.join(self.dst, rel)) for rel in self.tree(self.dst)}
        folders = identity(self.cur()), identity(self.cur("docs"))

        plan, result, _manifest = self.backup()

        self.assertTrue(plan.is_empty)
        self.assertFalse(plan.touches_hdd)
        self.assertIsNone(result)
        self.assertEqual(self.fingerprinted, [], "no file should even be opened")
        after = {rel: identity(os.path.join(self.dst, rel)) for rel in self.tree(self.dst)}
        self.assertEqual(before, after, "nothing on the HDD may change, the index included")
        self.assertEqual(folders, (identity(self.cur()), identity(self.cur("docs"))))


class OneFileChanged(Workspace):
    def setUp(self):
        super().setUp()
        self.write("top.txt", b"top")
        self.write("a/b/c/deep.txt", b"old contents")
        self.write("a/b/other.txt", b"other")
        self.write("z/far.txt", b"far away")
        self.backup()

    def test_only_that_file_is_written(self):
        untouched = {rel: identity(self.cur(rel)) for rel in ("top.txt", "a/b/other.txt", "z/far.txt")}
        self.write("a/b/c/deep.txt", b"new contents, longer")

        plan, result, _manifest = self.backup()

        self.assertEqual([op.rel for op in plan.copies], ["a/b/c/deep.txt"])
        self.assertEqual(plan.changed_files, 1)
        self.assertEqual(plan.reorganise_ops, 0)
        self.assertEqual(result.copied_files, 1)
        self.assertEqual(untouched, {rel: identity(self.cur(rel)) for rel in untouched})
        self.assertMirrored()

    def test_same_size_change_is_noticed(self):
        self.write("a/b/c/deep.txt", b"NEW contents")   # same length as before
        plan, _result, _manifest = self.backup()
        self.assertEqual([op.rel for op in plan.copies], ["a/b/c/deep.txt"])
        self.assertEqual(self.fingerprinted, ["a/b/c/deep.txt"])
        self.assertMirrored()

    def test_touched_but_identical_file_is_not_written(self):
        before = identity(self.cur("a/b/c/deep.txt"))
        self.touch("a/b/c/deep.txt")

        plan, result, manifest = self.backup()

        self.assertEqual(plan.copies, [])
        self.assertFalse(plan.touches_hdd)
        self.assertIsNone(result)
        self.assertEqual(list(plan.retimed), ["a/b/c/deep.txt"])
        self.assertEqual(before, identity(self.cur("a/b/c/deep.txt")))
        self.assertEqual(manifest.files["a/b/c/deep.txt"][1], os.stat(self.s("a/b/c/deep.txt")).st_mtime_ns)
        # and the new time is remembered, so the next run does not check it again
        plan, _result, _manifest = self.backup()
        self.assertTrue(plan.is_empty)
        self.assertEqual(self.fingerprinted, [])

    def test_new_file_in_new_folder(self):
        self.write("fresh/folder/new.txt", b"brand new")
        plan, result, _manifest = self.backup()
        self.assertEqual([op.rel for op in plan.copies], ["fresh/folder/new.txt"])
        self.assertEqual(result.copied_files, 1)
        self.assertMirrored()


class Deletions(Workspace):
    def setUp(self):
        super().setUp()
        self.write("keep.txt", b"keep")
        self.write("docs/report.txt", b"report")
        self.write("docs/notes.txt", b"notes")
        self.write("photos/2024/a.jpg", b"a" * 100)
        self.write("photos/2024/b.jpg", b"b" * 200)
        self.backup()

    def test_deleted_file_goes_to_dated_folder_with_its_path(self):
        before = identity(self.cur("docs/report.txt"))
        os.remove(self.s("docs/report.txt"))

        plan, result, manifest = self.backup(day="2026-03-05")

        self.assertEqual(plan.archive_files, [("docs/report.txt", 6)])
        self.assertEqual(plan.copies, [])
        self.assertFalse(os.path.exists(self.cur("docs/report.txt")))
        archived = self.arch("docs/report.txt", "2026-03-05")
        self.assertEqual(self.read(archived), b"report")
        self.assertEqual(before, identity(archived), "archiving must be a rename, not a copy")
        self.assertEqual(result.archived_files, 1)
        self.assertEqual(manifest.archives, {"2026-03-05": {"files": 1, "bytes": 6}})
        self.assertNotIn("docs/report.txt", manifest.files)
        self.assertMirrored()

    def test_deleted_folder_is_archived_in_one_rename(self):
        folder = identity(self.cur("photos"))
        shutil.rmtree(self.s("photos"))

        plan, result, manifest = self.backup()

        self.assertEqual(len(plan.archive_dirs), 1)
        self.assertEqual(plan.archive_dirs[0].rel, "photos")
        self.assertEqual(plan.archive_files, [])
        self.assertEqual(plan.rmdirs, [])
        self.assertEqual(folder[0], os.stat(self.arch("photos")).st_ino, "the folder itself was renamed")
        self.assertEqual(self.read(self.arch("photos/2024/b.jpg")), b"b" * 200)
        self.assertFalse(os.path.exists(self.cur("photos")))
        self.assertEqual(result.archived_files, 2)
        self.assertEqual(manifest.archives[DAY], {"files": 2, "bytes": 300})
        self.assertEqual(manifest.dirs, {"docs"})
        self.assertMirrored()

    def test_empty_folder_removed_from_the_mirror(self):
        os.makedirs(self.s("hollow/inside"))
        self.backup()
        self.assertTrue(os.path.isdir(self.cur("hollow/inside")))
        shutil.rmtree(self.s("hollow"))

        plan, _result, manifest = self.backup()

        self.assertEqual(plan.rmdirs, ["hollow/inside", "hollow"])
        self.assertFalse(os.path.exists(self.cur("hollow")))
        self.assertFalse(os.path.exists(self.arch("hollow")), "an empty folder is not worth archiving")
        self.assertNotIn("hollow", manifest.dirs)

    def test_same_path_archived_twice_in_a_day_keeps_both(self):
        os.remove(self.s("keep.txt"))
        self.backup()
        self.write("keep.txt", b"second life")
        self.backup()
        os.remove(self.s("keep.txt"))
        _plan, _result, manifest = self.backup()

        self.assertEqual(self.read(self.arch("keep.txt")), b"keep")
        self.assertEqual(self.read(self.arch("keep (2).txt")), b"second life")
        self.assertEqual(manifest.archives[DAY]["files"], 2)

    def test_folder_deleted_twice_in_a_day_merges(self):
        shutil.rmtree(self.s("docs"))
        self.backup()
        self.write("docs/later.txt", b"later")
        self.backup()
        shutil.rmtree(self.s("docs"))
        self.backup()

        self.assertEqual(set(os.listdir(self.arch("docs"))), {"report.txt", "notes.txt", "later.txt"})
        self.assertFalse(os.path.exists(self.cur("docs")))

    def test_file_replaced_by_folder_and_back(self):
        os.remove(self.s("keep.txt"))
        self.write("keep.txt/inside.txt", b"now a folder")
        self.backup()
        self.assertMirrored()
        self.assertEqual(self.read(self.arch("keep.txt")), b"keep")

        shutil.rmtree(self.s("keep.txt"))
        self.write("keep.txt", b"a file again")
        self.backup(day="2026-01-02")
        self.assertMirrored()
        self.assertEqual(self.read(self.arch("keep.txt/inside.txt", "2026-01-02")), b"now a folder")


class Moves(Workspace):
    def setUp(self):
        super().setUp()
        self.write("keep.txt", b"keep")
        self.write("inbox/video.mp4", b"v" * 4096)
        self.write("inbox/song.mp3", b"s" * 2048)
        self.write("projects/app/src/main.py", b"print('hi')")
        self.write("projects/app/src/util.py", b"def util(): pass")
        self.write("projects/app/README", b"read me")
        self.backup()

    def test_moved_file_is_renamed_not_recopied(self):
        before = identity(self.cur("inbox/video.mp4"))
        os.makedirs(self.s("media/films"))
        os.rename(self.s("inbox/video.mp4"), self.s("media/films/video.mp4"))

        plan, result, manifest = self.backup()

        self.assertEqual(plan.copies, [])
        self.assertEqual([(mv.old, mv.new) for mv in plan.file_moves], [("inbox/video.mp4", "media/films/video.mp4")])
        self.assertEqual(plan.archive_files, [])
        self.assertEqual(before, identity(self.cur("media/films/video.mp4")))
        self.assertEqual(result.moved_files, 1)
        self.assertEqual(result.copied_bytes, 0)
        self.assertFalse(os.path.exists(os.path.join(self.dst, DAY)), "a move is not a deletion")
        self.assertIn("media/films/video.mp4", manifest.files)
        self.assertMirrored()

    def test_renamed_file_is_renamed(self):
        before = identity(self.cur("inbox/song.mp3"))
        os.rename(self.s("inbox/song.mp3"), self.s("inbox/track 01.mp3"))
        plan, _result, _manifest = self.backup()
        self.assertEqual(plan.copies, [])
        self.assertEqual(before, identity(self.cur("inbox/track 01.mp3")))
        self.assertMirrored()

    def test_moved_folder_is_one_rename(self):
        folder = os.stat(self.cur("projects/app")).st_ino
        files = {rel: identity(self.cur("projects/app/" + rel)) for rel in ("src/main.py", "src/util.py", "README")}
        os.makedirs(self.s("archive/old"))
        os.rename(self.s("projects/app"), self.s("archive/old/app"))

        plan, result, manifest = self.backup()

        self.assertEqual([(dm.old, dm.new) for dm in plan.dir_moves], [("projects/app", "archive/old/app")])
        self.assertEqual(plan.file_moves, [])
        self.assertEqual(plan.copies, [])
        self.assertEqual(plan.moved_files, 3)
        self.assertEqual(result.moved_dirs, 1)
        self.assertEqual(folder, os.stat(self.cur("archive/old/app")).st_ino)
        self.assertEqual(files, {rel: identity(self.cur("archive/old/app/" + rel)) for rel in files})
        self.assertIn("archive/old/app/src", manifest.dirs)
        self.assertNotIn("projects/app", manifest.dirs)
        self.assertMirrored()
        plan, _result, _manifest = self.backup()
        self.assertTrue(plan.is_empty)

    def test_renamed_top_folder_with_extra_new_file(self):
        os.rename(self.s("projects"), self.s("work"))
        self.write("work/app/NEW.txt", b"added after the move")

        plan, result, _manifest = self.backup()

        self.assertEqual([(dm.old, dm.new) for dm in plan.dir_moves], [("projects", "work")])
        self.assertEqual([op.rel for op in plan.copies], ["work/app/NEW.txt"])
        self.assertEqual(result.copied_files, 1)
        self.assertMirrored()

    def test_folder_moved_with_one_file_changed_renames_the_intact_part(self):
        os.rename(self.s("projects"), self.s("work"))
        self.write("work/app/README", b"rewritten after the move")

        plan, result, _manifest = self.backup()

        # "projects" as a whole no longer matches, but its untouched subfolder still does
        self.assertEqual([(dm.old, dm.new) for dm in plan.dir_moves], [("projects/app/src", "work/app/src")])
        self.assertEqual(plan.file_moves, [])
        self.assertEqual([op.rel for op in plan.copies], ["work/app/README"])
        self.assertEqual([ad.rel for ad in plan.archive_dirs], ["projects"])
        self.assertEqual(result.copied_files, 1)
        self.assertEqual(self.read(self.arch("projects/app/README")), b"read me")
        self.assertMirrored()

    def test_files_moved_out_of_a_folder_that_was_then_deleted(self):
        os.makedirs(self.s("saved"))
        os.rename(self.s("projects/app/src/main.py"), self.s("saved/main.py"))
        shutil.rmtree(self.s("projects"))

        plan, _result, _manifest = self.backup()

        self.assertEqual([(mv.old, mv.new) for mv in plan.file_moves], [("projects/app/src/main.py", "saved/main.py")])
        self.assertEqual(plan.copies, [])
        self.assertEqual(plan.deleted_files, 2)
        self.assertFalse(os.path.exists(self.arch("projects/app/src/main.py")), "it moved; it was not deleted")
        self.assertTrue(os.path.exists(self.arch("projects/app/src/util.py")))
        self.assertMirrored()

    def test_identical_twins_are_not_confused(self):
        stamp = 1_650_000_000 * SECOND
        self.write("twins/one.dat", b"same bytes", stamp)
        self.write("twins/two.dat", b"same bytes", stamp)
        self.backup()
        os.rename(self.s("twins/one.dat"), self.s("twins/uno.dat"))
        os.rename(self.s("twins/two.dat"), self.s("twins/dos.dat"))
        plan, _result, _manifest = self.backup()
        self.assertEqual(plan.copies, [])
        self.assertEqual(len(plan.file_moves), 2)
        self.assertMirrored()

    def test_same_size_and_time_but_different_content_is_copied(self):
        stamp = os.stat(self.s("inbox/video.mp4")).st_mtime_ns
        os.remove(self.s("inbox/video.mp4"))
        self.write("elsewhere/other.mp4", b"x" * 4096, stamp)
        plan, _result, _manifest = self.backup()
        self.assertEqual(plan.file_moves, [])
        self.assertEqual([op.rel for op in plan.copies], ["elsewhere/other.mp4"])
        self.assertEqual(plan.archive_files, [("inbox/video.mp4", 4096)])
        self.assertMirrored()

    def test_move_falls_back_to_copy_when_the_hdd_disagrees(self):
        os.makedirs(self.s("media"))
        os.rename(self.s("inbox/video.mp4"), self.s("media/video.mp4"))
        os.remove(self.cur("inbox/video.mp4"))   # someone deleted it from the HDD by hand

        plan, result, manifest = self.backup()

        self.assertEqual(len(plan.file_moves), 1)
        self.assertEqual(result.moved_files, 0)
        self.assertEqual(result.copied_files, 1)
        self.assertNotIn("inbox/video.mp4", manifest.files)
        self.assertMirrored()


class Interruptions(Workspace):
    def setUp(self):
        super().setUp()
        for i in range(6):
            self.write("f%d.bin" % i, bytes([i]) * 1000)

    def test_clean_stop_then_resume(self):
        _plan, result, manifest = self.backup(events=StopAfter(2))
        self.assertTrue(result.stopped)
        self.assertEqual(result.copied_files, 2)
        self.assertEqual(result.remaining_files, 4)
        self.assertEqual(len(manifest.files), 2)

        plan, result, _manifest = self.backup()
        self.assertEqual(plan.new_files, 4)
        self.assertEqual(result.copied_files, 4)
        self.assertFalse(result.stopped)
        self.assertMirrored()

    def test_crash_without_saving_the_index_does_not_recopy(self):
        self.backup(events=StopAfter(3), save=False)   # power cut: files written, index lost
        self.assertFalse(os.path.exists(index_path(self.dst)))
        done = {rel: identity(self.cur(rel)) for rel in self.tree(self.cur())}
        self.assertEqual(len(done), 3)

        plan, result, manifest = self.backup()

        self.assertEqual(plan.new_files, 6)
        self.assertEqual(result.resumed_files, 3)
        self.assertEqual(result.copied_files, 3)
        self.assertEqual(done, {rel: identity(self.cur(rel)) for rel in done}, "finished files were rewritten")
        self.assertEqual(len(manifest.files), 6)
        self.assertMirrored()

    def test_abort_inside_a_file_leaves_nothing_half_written(self):
        _plan, result, manifest = self.backup(events=AbortMidFile())
        self.assertTrue(result.stopped)
        self.assertEqual(result.copied_files, 0)
        self.assertEqual(self.tree(self.cur()), {})
        self.assertEqual(manifest.files, {})

    def test_abort_never_damages_the_previous_copy(self):
        self.backup()
        self.write("f0.bin", b"changed and longer than before")
        _plan, result, _manifest = self.backup(events=AbortMidFile())
        self.assertTrue(result.stopped)
        self.assertEqual(self.read(self.cur("f0.bin")), bytes([0]) * 1000)
        self.assertFalse(os.path.exists(self.cur("f0.bin") + TMP_SUFFIX))

    def test_unreadable_source_file_is_skipped_and_retried(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions as a normal user")
        os.chmod(self.s("f3.bin"), 0)
        _plan, result, manifest = self.backup()
        self.assertEqual([rel for rel, _why in result.skipped], ["f3.bin"])
        self.assertEqual(result.copied_files, 5)
        self.assertNotIn("f3.bin", manifest.files)

        os.chmod(self.s("f3.bin"), 0o644)
        plan, result, _manifest = self.backup()
        self.assertEqual([op.rel for op in plan.copies], ["f3.bin"])
        self.assertMirrored()


class Safety(Workspace):
    def test_exclusions(self):
        self.write("keep.txt")
        self.write("cache.tmp")
        self.write("$RECYCLE.BIN/S-1-5/junk.txt")
        self.write("node_modules/pkg/index.js")
        self.write("Games/Cache/blob.bin")
        self.write("Games/save.dat")
        self.write("other/Cache/kept.bin")

        self.backup(excludes=["$RECYCLE.BIN", "*.TMP", "node_modules", "games/cache"])

        self.assertEqual(set(self.tree(self.cur())), {"keep.txt", "Games/save.dat", "other/Cache/kept.bin"})

    def test_symlinks_are_not_followed(self):
        if os.name == "nt":
            self.skipTest("symlinks need privileges on Windows")
        self.write("real/file.txt", b"real")
        os.symlink(self.s("real"), self.s("loop"))
        os.symlink(self.s("real/file.txt"), self.s("alias.txt"))
        plan, _result, _manifest = self.backup()
        self.assertEqual(plan.new_files, 1)
        self.assertEqual(set(self.tree(self.cur())), {"real/file.txt"})

    def test_unreadable_folder_is_left_alone_not_archived(self):
        if os.name == "nt" or os.geteuid() == 0:
            self.skipTest("needs POSIX permissions as a normal user")
        self.write("open/a.txt", b"a")
        self.write("locked/secret.txt", b"secret")
        self.write("locked/sub/more.txt", b"more")
        self.backup()
        os.chmod(self.s("locked"), 0)
        self.addCleanup(os.chmod, self.s("locked"), 0o755)

        plan, _result, manifest = self.backup()

        self.assertIn("locked", plan.unreadable)
        self.assertEqual(plan.deleted_files, 0)
        self.assertEqual(plan.archive_dirs, [])
        self.assertEqual(plan.rmdirs, [])
        self.assertFalse(plan.touches_hdd)
        self.assertEqual(self.read(self.cur("locked/sub/more.txt")), b"more")
        self.assertIn("locked/secret.txt", manifest.files)

    def test_deleted_share_is_reported_for_the_mass_delete_guard(self):
        for i in range(10):
            self.write("f%d.txt" % i)
        self.backup()
        for i in range(7):
            os.remove(self.s("f%d.txt" % i))
        _manifest, plan = self.plan()
        self.assertEqual(plan.deleted_files, 7)
        self.assertEqual(plan.index_files, 10)

    def test_space_needed(self):
        self.write("a.bin", b"a" * 1000)
        self.write("b.bin", b"b" * 500)
        _manifest, plan = self.plan()
        self.assertEqual(plan.space_needed, 1500)
        self.backup()
        self.write("a.bin", b"a" * 1200)
        _manifest, plan = self.plan()
        # 200 bytes of growth, plus room for the new copy while the old one still exists
        self.assertEqual(plan.space_needed, 200 + 1200)

    def test_stale_folder_in_index_is_recreated(self):
        self.write("docs/a.txt", b"a")
        self.backup()
        shutil.rmtree(self.cur("docs"))   # removed from the HDD behind the program's back
        self.write("docs/b.txt", b"b")
        _plan, result, _manifest = self.backup()
        self.assertEqual(result.copied_files, 1)
        self.assertEqual(self.read(self.cur("docs/b.txt")), b"b")


class Index(Workspace):
    def test_round_trip(self):
        manifest = Manifest()
        manifest.files = {"a/b.txt": [5, 123456789, "abcd"], "c.txt": [0, 1, None]}
        manifest.dirs = {"a"}
        manifest.archives = {"2026-01-01": {"files": 1, "bytes": 9}}
        manifest.source_id = "1A2B-3C4D"
        manifest.rate = 1.5e8
        manifest.add_history({"ts": 1, "copied_files": 2})
        manifest.save(self.dst)

        loaded = Manifest.load(self.dst)
        self.assertTrue(loaded.loaded)
        self.assertEqual(loaded.files, manifest.files)
        self.assertEqual(loaded.dirs, {"a"})
        self.assertEqual(loaded.archives, manifest.archives)
        self.assertEqual(loaded.source_id, "1A2B-3C4D")
        self.assertEqual(loaded.rate, 1.5e8)
        self.assertEqual(loaded.history, [{"ts": 1, "copied_files": 2}])

    def test_missing_index_is_an_empty_backup(self):
        loaded = Manifest.load(self.dst)
        self.assertFalse(loaded.loaded)
        self.assertEqual(loaded.files, {})

    def test_names_that_are_not_valid_unicode_survive(self):
        manifest = Manifest()
        odd = "bad-\udcff-name.txt"   # how Python shows an undecodable byte in a file name
        manifest.files = {odd: [1, 2, None]}
        manifest.save(self.dst)
        self.assertEqual(list(Manifest.load(self.dst).files), [odd])

    def test_damaged_index_falls_back_to_the_previous_one(self):
        first = Manifest()
        first.files = {"old.txt": [1, 2, None]}
        first.save(self.dst)
        first.files["new.txt"] = [3, 4, None]
        first.save(self.dst)
        with open(index_path(self.dst), "wb") as handle:
            handle.write(b"not gzip at all")
        self.assertEqual(set(Manifest.load(self.dst).files), {"old.txt"})

    def test_damaged_index_with_no_spare_is_reported(self):
        os.makedirs(os.path.dirname(index_path(self.dst)))
        with open(index_path(self.dst), "wb") as handle:
            handle.write(b"garbage")
        with self.assertRaises(ManifestError):
            Manifest.load(self.dst)


class Rebuild(Workspace):
    def setUp(self):
        super().setUp()
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")
        self.write("docs/c.txt", b"charlie")

    def test_adopts_a_backup_made_by_hand(self):
        shutil.copytree(self.src, self.cur())   # copytree keeps modified times

        manifest, stats = rebuild.rebuild(self.src, self.dst, ())
        manifest.save(self.dst)

        self.assertEqual(stats.matched, 3)
        self.assertEqual(stats.different, 0)
        plan, _result, _manifest = self.backup()
        self.assertTrue(plan.is_empty, "an adopted backup must not be copied again")

    def test_sorts_out_a_mixed_state(self):
        shutil.copytree(self.src, self.cur())
        later = os.stat(self.cur("a.txt")).st_mtime_ns + 500 * SECOND
        os.utime(self.cur("a.txt"), ns=(later, later))              # same contents, different date
        with open(self.cur("docs/b.txt"), "wb") as handle:           # same size, different contents
            handle.write(b"BRAVO")
        os.utime(self.cur("docs/b.txt"), ns=(later, later))
        with open(self.cur("docs/orphan.txt"), "wb") as handle:      # no longer on the source
            handle.write(b"orphan")
        with open(self.cur("docs/half.bin" + TMP_SUFFIX), "wb") as handle:
            handle.write(b"unfinished")
        self.write("docs/unbacked.txt", b"new")
        os.makedirs(os.path.join(self.dst, "2025-12-25", "old"))
        with open(os.path.join(self.dst, "2025-12-25", "old", "x.txt"), "wb") as handle:
            handle.write(b"12345")

        manifest, stats = rebuild.rebuild(self.src, self.dst, ())
        manifest.save(self.dst)

        self.assertEqual((stats.matched, stats.verified, stats.different), (1, 1, 1))
        self.assertEqual((stats.only_hdd, stats.only_source, stats.leftovers), (1, 1, 1))
        self.assertEqual(manifest.archives, {"2025-12-25": {"files": 1, "bytes": 5}})
        self.assertFalse(os.path.exists(self.cur("docs/half.bin" + TMP_SUFFIX)))

        plan, _result, _manifest = self.backup()
        self.assertEqual(sorted(op.rel for op in plan.copies), ["docs/b.txt", "docs/unbacked.txt"])
        self.assertEqual(plan.archive_files, [("docs/orphan.txt", 6)])
        self.assertMirrored()
        self.assertEqual(self.read(self.arch("docs/orphan.txt")), b"orphan")


if __name__ == "__main__":
    sys.exit(unittest.main())
