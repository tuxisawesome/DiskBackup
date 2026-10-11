"""The smaller pieces: time estimates, terminal drawing, exclusions, settings."""

import io
import os
import unittest

from diskbackup import config, osutil, progress, term
from diskbackup.scanner import Excluder

from .helpers import Workspace

MB = 1024 * 1024


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Estimates(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def copy_file(self, est, size, rate, overhead, chunk=8 * MB):
        """Simulate copying one file on a drive with the given speed and per-file cost."""
        est.file_start()
        self.clock.now += overhead
        left = size
        while left > 0:
            part = min(chunk, left)
            self.clock.now += part / rate
            est.add_bytes(part)
            left -= part
        est.file_end()

    def test_large_file_eta_follows_throughput(self):
        est = progress.Estimator(1000 * MB, 1, clock=self.clock)
        est.file_start()
        for _ in range(25):
            self.clock.now += 0.08          # 8 MB every 80 ms = 100 MB/s
            est.add_bytes(8 * MB)
        self.assertAlmostEqual(est.rate / MB, 100, delta=1)
        self.assertAlmostEqual(est.eta(), 8.0, delta=0.3)
        self.assertAlmostEqual(est.fraction(), 0.2)

    def test_small_files_are_costed_per_file_not_per_byte(self):
        # 100 MB/s drive where every file also costs 50 ms to create
        files = [200 * MB] + [10_000] * 400
        est = progress.Estimator(sum(files), len(files), clock=self.clock)
        self.copy_file(est, files[0], 100 * MB, 0.05)
        for size in files[1:201]:
            self.copy_file(est, size, 100 * MB, 0.05)
        # 200 tiny files left: about 2 MB of data (0.02 s) but 10 s of overhead
        self.assertAlmostEqual(est.overhead, 0.05, delta=0.01)
        self.assertAlmostEqual(est.eta(), 10.0, delta=1.5)

    def test_falls_back_to_the_last_runs_figures(self):
        est = progress.Estimator(500 * MB, 10, prior_rate=50 * MB, prior_overhead=0.1, clock=self.clock)
        self.assertAlmostEqual(est.eta(), 500 / 50 + 10 * 0.1)

    def test_no_figures_at_all(self):
        est = progress.Estimator(500 * MB, 10, clock=self.clock)
        self.assertIsNone(est.eta())

    def test_pauses_do_not_count(self):
        est = progress.Estimator(160 * MB, 1, clock=self.clock)
        est.file_start()
        for _ in range(10):
            self.clock.now += 0.08
            est.add_bytes(8 * MB)
        before = est.eta()
        est.pause()
        self.clock.now += 3600
        est.resume()
        self.assertAlmostEqual(est.now(), 0.8)
        self.assertAlmostEqual(est.eta(), before)
        self.clock.now += 0.08
        est.add_bytes(8 * MB)
        self.assertAlmostEqual(est.rate / MB, 100, delta=1)

    def test_skipped_work_is_removed_from_the_total(self):
        est = progress.Estimator(100, 4, clock=self.clock)
        est.discount(25)
        self.assertEqual((est.total_bytes, est.total_files), (75, 3))
        est.add_work(10, 1)
        self.assertEqual((est.total_bytes, est.total_files), (85, 4))

    def test_finished(self):
        est = progress.Estimator(10, 1, clock=self.clock)
        est.file_start()
        self.clock.now += 1
        est.add_bytes(10)
        est.file_end()
        self.assertEqual(est.eta(), 0.0)
        self.assertEqual(est.fraction(), 1.0)

    def test_estimate_before_starting(self):
        self.assertIsNone(progress.estimate(100, 1, None, None))
        self.assertAlmostEqual(progress.estimate(1000, 10, 100, 0.5), 15.0)


class Drawing(unittest.TestCase):
    def plain(self, charset="ascii"):
        return term.Term(io.StringIO(), depth=0, charset=charset)

    def test_formatting(self):
        self.assertEqual(term.human_bytes(0), "0 B")
        self.assertEqual(term.human_bytes(1023), "1023 B")
        self.assertEqual(term.human_bytes(1536), "1.50 KB")
        self.assertEqual(term.human_bytes(852.3 * MB), "852 MB")
        self.assertEqual(term.human_bytes(3 * 1024 ** 4), "3.00 TB")
        self.assertEqual(term.human_duration(42), "42s")
        self.assertEqual(term.human_duration(192), "3m 12s")
        self.assertEqual(term.human_duration(3900), "1h 05m")
        self.assertEqual(term.plural(1, "file"), "1 file")
        self.assertEqual(term.plural(48201, "file"), "48,201 files")
        self.assertEqual(term.human_ago(1000, now=1000 + 7 * 86400), "7 days ago")

    def test_widths_ignore_colour_and_count_wide_characters(self):
        self.assertEqual(term.cell_width("\x1b[1;38;2;1;2;3mabc\x1b[0m"), 3)
        self.assertEqual(term.cell_width("日本語"), 6)
        self.assertEqual(term.cell_width("é"), 1)

    def test_clipping(self):
        self.assertEqual(term.clip("abcdefghij", 6, "..."), "abc...")
        self.assertEqual(term.clip("short", 10), "short")
        clipped = term.clip_middle("Photos/2026/holiday/clip-003.mov", 20, "...")
        self.assertEqual(len(clipped), 20)
        self.assertTrue(clipped.endswith("-003.mov"))
        self.assertTrue(clipped.startswith("Photos"))
        self.assertLessEqual(term.text_width(term.clip("日本語のファイル名", 7)), 7)

    def test_clipping_coloured_text_keeps_codes_whole(self):
        text = "\x1b[31mred\x1b[0m and \x1b[32mgreen\x1b[0m"
        cut = term.clip_ansi(text, 6)
        self.assertEqual(term.cell_width(cut), 6)
        self.assertTrue(cut.endswith(term.RESET))
        self.assertEqual(term.ANSI_RE.sub("", cut), "red an")
        self.assertEqual(term.clip_ansi(text, 50), text)

    def test_unprintable_names_are_made_safe(self):
        self.assertEqual(term.clean("ok name.txt"), "ok name.txt")
        self.assertEqual(term.clean("bad\x1b[2Jname\n"), "bad?[2Jname?")
        self.assertEqual(term.clean("broken-\udcff"), "broken-?")
        self.assertEqual(term.clean("日本語"), "日本語")

    def test_ascii_fallback_is_pure_ascii(self):
        t = self.plain()
        lines = t.panel("Title", ["one", t.bar(0.5, 10), t.spark([1, 5, 3], 8)], width=30)
        text = "\n".join(lines)
        self.assertTrue(text.isascii(), text)
        self.assertNotIn("\x1b", text)
        self.assertEqual(t.bar(0.5, 10), "#####.....")
        self.assertEqual(t.bar(0, 4), "....")
        self.assertEqual(t.bar(1, 4), "####")

    def test_panel_lines_are_all_the_same_width(self):
        for charset, depth in (("ascii", 0), ("safe", 16), ("fancy", 256), ("fancy", term.TRUECOLOR)):
            t = term.Term(io.StringIO(), depth=depth, charset=charset)
            lines = t.panel("A title", ["short", "x" * 200, t.bar(0.37, 20), t.title("日本語")], width=40)
            self.assertEqual({term.cell_width(line) for line in lines}, {40}, (charset, depth))

    def test_bar_width_is_exact_at_every_colour_depth(self):
        for charset, depth in (("safe", 16), ("fancy", 16), ("fancy", 256), ("fancy", term.TRUECOLOR)):
            t = term.Term(io.StringIO(), depth=depth, charset=charset)
            for fraction in (0, 0.01, 0.333, 0.5, 0.999, 1, 1.7, -1):
                self.assertEqual(term.cell_width(t.bar(fraction, 23)), 23, (charset, depth, fraction))

    def test_no_colour_codes_when_colour_is_off(self):
        t = self.plain("fancy")
        for text in (t.title("x"), t.gradient("abc"), t.bar(0.4, 8), t.err("x"), t.spark([1, 2], 4)):
            self.assertNotIn("\x1b", text)

    def test_each_colour_depth_only_uses_codes_that_depth_understands(self):
        def codes(depth):
            t = term.Term(io.StringIO(), depth=depth, charset="fancy")
            return t.title("x") + t.gradient("abc") + t.bar(0.4, 8) + t.err("x") + t.spark([1, 2], 4)

        self.assertIn("38;2;", codes(term.TRUECOLOR))
        self.assertIn("38;5;", codes(256))
        self.assertNotIn(";2;", codes(256).replace("38;5;", "").replace("48;5;", ""))
        basic = codes(16)
        self.assertNotIn("38;", basic)
        self.assertNotIn("48;", basic)
        self.assertIn("\x1b[35m", basic)

    def test_table_alignment(self):
        t = self.plain()
        self.assertEqual(t.table([["a", "1"], ["bbb", "22"]], "<>"), ["a     1", "bbb  22"])

    def test_live_is_silent_when_not_a_terminal(self):
        t = self.plain()
        live = term.Live(t)
        live.start()
        live.update(["line"])
        live.stop()
        self.assertEqual(t.out.getvalue(), "")

    def test_piped_output_gets_no_colour(self):
        saved = {key: os.environ.pop(key, None) for key in ("DISKBACKUP_COLOR", "NO_COLOR")}
        try:
            self.assertEqual(term.detect_depth(io.StringIO()), 0)
        finally:
            os.environ.update({key: value for key, value in saved.items() if value is not None})


class Exclusions(unittest.TestCase):
    def test_patterns(self):
        skip = Excluder(["$RECYCLE.BIN", "*.tmp", "Games/Cache", " ", "\\Videos\\Raw\\"])
        self.assertTrue(skip("$Recycle.Bin", "$Recycle.Bin"))
        self.assertTrue(skip("x.TMP", "a/b/x.TMP"))
        self.assertTrue(skip("Cache", "Games/Cache"))
        self.assertFalse(skip("Cache", "Other/Cache"))
        self.assertTrue(skip("Raw", "Videos/Raw"))
        self.assertFalse(skip("keep.txt", "a/keep.txt"))
        self.assertFalse(Excluder([])("anything", "anything"))


class VolumeIds(unittest.TestCase):
    def test_same_id(self):
        self.assertTrue(osutil.same_id("1A2B-3C4D", "1a2b-3c4d"))
        self.assertTrue(osutil.same_id("1A2B3C4D", "1A2B-3C4D"))
        # Linux shows an NTFS serial in full, Windows only its second half
        self.assertTrue(osutil.same_id("01D9A1B21A2B3C4D", "1A2B-3C4D"))
        self.assertFalse(osutil.same_id("1A2B-3C4D", "1A2B-3C4E"))
        self.assertFalse(osutil.same_id("", "1A2B-3C4D"))
        self.assertFalse(osutil.same_id(None, None))

    def test_is_inside(self):
        self.assertTrue(osutil.is_inside("/a/b/c", "/a/b"))
        self.assertTrue(osutil.is_inside("/a/b", "/a/b"))
        self.assertFalse(osutil.is_inside("/a/bc", "/a/b"))
        self.assertFalse(osutil.is_inside("/a", "/a/b"))


class Settings(Workspace):
    def test_round_trip(self):
        path = os.path.join(self.root, "cfg", "config.json")
        cfg = config.Config()
        cfg.set_source(self.src)
        cfg.set_dest(self.dst)
        cfg.excludes.append("*.iso")
        config.save(cfg, path)

        loaded = config.load(path)
        self.assertEqual(loaded.source, self.src)
        self.assertEqual(loaded.dest, self.dst)
        self.assertEqual(loaded.excludes, config.DEFAULT_EXCLUDES + ["*.iso"])
        self.assertTrue(loaded.ready)

    def test_missing_or_broken_file_gives_defaults(self):
        self.assertFalse(config.load(os.path.join(self.root, "nope.json")).ready)
        broken = os.path.join(self.root, "broken.json")
        with open(broken, "w") as handle:
            handle.write("{not json")
        self.assertEqual(config.load(broken).excludes, config.DEFAULT_EXCLUDES)

    def test_locate(self):
        self.assertEqual(config.locate(self.src, "", ""), (self.src, "ok"))
        self.assertEqual(config.locate(os.path.join(self.root, "gone"), "", ""), (None, "missing"))
        # a path that exists but sits on some other drive than the one configured
        vol = osutil.volume_of(self.src)
        if vol and vol.id:
            self.assertEqual(config.locate(self.src, "FFFF-0000", ""), (None, "different"))


if __name__ == "__main__":
    unittest.main()
