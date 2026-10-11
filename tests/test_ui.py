"""Drive the real menus with typed answers, as a user would."""

import io
import os
import shutil
import time
from unittest import mock

from diskbackup import ui
from diskbackup.manifest import Manifest

from .helpers import Workspace

TODAY = time.strftime("%Y-%m-%d")


class Menus(Workspace):
    def setUp(self):
        super().setUp()
        self.cfg = os.path.join(self.root, "settings", "config.json")
        self.hdd = os.path.join(self.dst, "Backup")

    def drive(self, *answers):
        """Start the program, type each answer in turn, and return everything it printed."""
        typed = iter(answers)

        def fake_input(prompt=""):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError

        out = io.StringIO()
        env = {"DISKBACKUP_COLOR": "0", "DISKBACKUP_CHARS": "ascii"}
        with mock.patch("builtins.input", fake_input), mock.patch("sys.stdout", out), \
                mock.patch.dict(os.environ, env):
            self.assertEqual(ui.main(["--config", self.cfg]), 0)
        text = out.getvalue()
        self.assertTrue(text.isascii(), "plain mode must stay plain")
        return text

    def setup_answers(self):
        return [self.src, self.dst, "Backup", ""]   # source path, HDD path, folder name, Enter

    def hdd_cur(self, rel):
        return os.path.join(self.hdd, "current", *rel.split("/"))

    def test_first_run_setup_then_backup(self):
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")

        text = self.drive(*self.setup_answers(), "1", "", "", "q")

        self.assertIn("First-time setup", text)
        self.assertIn("First backup", text)
        self.assertIn("Backup complete", text)
        self.assertIn("2 files", text)
        with open(self.hdd_cur("docs/b.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"bravo")
        manifest = Manifest.load(self.hdd)
        self.assertEqual(len(manifest.files), 2)
        self.assertEqual(len(manifest.history), 1)
        self.assertEqual(manifest.history[0]["copied_files"], 2)
        logs = os.listdir(os.path.join(self.hdd, ".diskbackup", "logs"))
        self.assertEqual(len(logs), 1)

    def test_whole_life_of_a_backup(self):
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")
        self.write("docs/c.txt", b"charlie")
        self.drive(*self.setup_answers(), "1", "", "", "q")

        # nothing changed
        text = self.drive("1", "", "q")
        self.assertIn("Nothing has changed", text)
        self.assertEqual(len(Manifest.load(self.hdd).history), 1, "an idle run writes nothing, not even history")

        # preview only: shows the change, writes nothing
        self.write("docs/b.txt", b"bravo, edited")
        os.remove(self.s("a.txt"))
        text = self.drive("2", "v", "", "", "q")
        self.assertIn("Changed", text)
        self.assertIn("Overwrite on the HDD", text)
        self.assertTrue(os.path.exists(self.hdd_cur("a.txt")))
        with open(self.hdd_cur("docs/b.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"bravo")

        # cancel at the confirmation
        self.drive("1", "q", "q")
        self.assertTrue(os.path.exists(self.hdd_cur("a.txt")))

        # the real thing
        text = self.drive("1", "", "", "q")
        self.assertIn("Backup complete", text)
        self.assertFalse(os.path.exists(self.hdd_cur("a.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.hdd, TODAY, "a.txt")))
        with open(self.hdd_cur("docs/b.txt"), "rb") as handle:
            self.assertEqual(handle.read(), b"bravo, edited")

        # history lists both runs
        text = self.drive("3", "", "q")
        self.assertIn("Across 2 runs", text)

        # archive manager: a wrong confirmation keeps it, the right one removes it
        text = self.drive("4", "1", "nope", "", "q")
        self.assertIn(TODAY, text)
        self.assertTrue(os.path.isdir(os.path.join(self.hdd, TODAY)))
        self.drive("4", "1", "delete", "", "q")
        self.assertFalse(os.path.exists(os.path.join(self.hdd, TODAY)))
        self.assertEqual(Manifest.load(self.hdd).archives, {})

    def test_exclusions_can_be_edited(self):
        self.write("keep.txt")
        self.write("skip.iso")
        self.drive(*self.setup_answers(), "5", "3", "*.iso", "", "", "1", "", "", "q")
        self.assertTrue(os.path.exists(self.hdd_cur("keep.txt")))
        self.assertFalse(os.path.exists(self.hdd_cur("skip.iso")))

    def test_refuses_backup_folder_inside_the_source(self):
        text = self.drive(self.src, self.src, "Backup")
        self.assertIn("cannot be inside", text)
        self.assertFalse(os.path.exists(os.path.join(self.src, "Backup")))

    def test_empty_source_never_empties_the_backup(self):
        self.write("a.txt", b"alpha")
        self.drive(*self.setup_answers(), "1", "", "", "q")
        os.remove(self.s("a.txt"))   # as if the wrong, empty drive were mounted there

        text = self.drive("1", "", "q")

        self.assertIn("Stopped for safety", text)
        self.assertTrue(os.path.exists(self.hdd_cur("a.txt")))

    def test_mass_deletion_needs_a_typed_yes(self):
        for i in range(60):
            self.write("f%02d.txt" % i, b"x")
        self.drive(*self.setup_answers(), "1", "", "", "q")
        for i in range(55):
            os.remove(self.s("f%02d.txt" % i))

        text = self.drive("1", "", "q")   # Enter is not "yes"
        self.assertIn("92% of the backup", text)
        self.assertTrue(os.path.exists(self.hdd_cur("f00.txt")))

        self.drive("1", "yes", "", "", "q")
        self.assertFalse(os.path.exists(self.hdd_cur("f00.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.hdd, TODAY, "f00.txt")))

    def test_missing_drive_is_explained(self):
        self.write("a.txt")
        self.drive(*self.setup_answers(), "q")
        shutil.rmtree(self.hdd)
        text = self.drive("1", "", "q")
        self.assertIn("backup drive is not connected", text)

    def test_existing_copy_without_index_asks_for_rebuild_then_adopts(self):
        self.write("a.txt", b"alpha")
        self.write("docs/b.txt", b"bravo")
        self.drive(*self.setup_answers(), "q")
        shutil.copytree(self.src, os.path.join(self.hdd, "current"))

        text = self.drive("1", "", "q")
        self.assertIn("Rebuild index", text)
        self.assertIn("Existing files found", text)

        text = self.drive("6", "y", "", "1", "", "q")
        self.assertIn("Index rebuilt", text)
        self.assertIn("Nothing has changed", text)
