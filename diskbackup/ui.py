"""The interactive terminal interface: every screen the user sees."""

import argparse
import os
import signal
import time
from collections import Counter, deque

from . import __version__, config, osutil, planner, progress, rebuild, scanner
from .executor import BackupError, Events, Executor, Stopped, history_entry, log_lines
from .manifest import CURRENT, Manifest, ManifestError, write_log
from .term import (Live, Term, cell_width, clean, clip_ansi, human_ago, human_bytes, human_count,
                   human_duration, pad, plural)

PHASES = (("scan", "Scan"), ("compare", "Compare"), ("reorganise", "Reorganise"),
          ("copy", "Copy"), ("save", "Save index"))
REBUILD_PHASES = (("scan", "Scan both drives"), ("compare", "Compare"), ("save", "Save index"))
BIG_FILE = 64 * 1024 * 1024       # files this large get their own progress bar
MASS_DELETE_SHARE = 0.20
MASS_DELETE_MIN = 50


class Quit(Exception):
    """Leave the program."""


class Cancelled(Exception):
    """The user backed out before anything was written."""


def show(rel):
    """An index path the way this OS writes paths, safe to print."""
    return clean(rel.replace("/", os.sep))


def when(timestamp):
    return time.strftime("%d %b %Y %H:%M", time.localtime(timestamp)).lstrip("0")


# =========================================================================== live progress

class RunView(Events):
    """The live display for a run, and the executor's window onto the keyboard."""

    FRAME = 0.25         # seconds between redraws
    PLAIN_EVERY = 10.0   # seconds between status lines when output is not a terminal

    def __init__(self, term, title, phases=PHASES, done=()):
        self.t = term
        self.title = title
        self.phases = phases
        self.done = set(done)
        self.current = None
        self.live = Live(term)
        self.keys = osutil.KeyReader()
        self._last_frame = 0.0
        self._last_plain = time.monotonic()
        self._old_sigint = None

        self.scan_files = 0
        self.scan_bytes = 0
        self.scan_dir = ""
        self.scan_label = "Reading the SSD"
        self.expected_files = 0
        self._scan_started = time.monotonic()

        self.check_files = 0
        self.check_bytes = 0
        self.check_done = 0
        self.check_rel = ""

        self.ops_total = 0
        self.ops_done = 0

        self.est = None
        self.work = (0, 0)
        self.prior = (None, None)
        self._extra = [0, 0]
        self.cur_rel = ""
        self.cur_size = 0
        self.cur_done = 0
        self.recent = deque(maxlen=4)
        self.skipped = 0
        self.speeds = deque(maxlen=24)
        self._last_sample = 0.0
        self._eta = None
        self.note = ""

        self.paused = False
        self.stop = False
        self.abort = False

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self):
        self.keys.__enter__()
        self.live.start()
        try:
            self._old_sigint = signal.signal(signal.SIGINT, self._on_sigint)
        except ValueError:
            self._old_sigint = None
        return self

    def __exit__(self, *exc):
        if self._old_sigint is not None:
            signal.signal(signal.SIGINT, self._old_sigint)
        if exc[0] is None:
            self._draw()
        self.live.stop()
        self.keys.__exit__()
        return False

    # ------------------------------------------------------------------ stop / pause

    def _writing(self):
        return self.current in ("reorganise", "copy", "save")

    def _request_stop(self):
        if not self._writing():
            raise Cancelled()
        if self.stop:
            self.abort = True
        self.stop = True
        if self.paused:
            self._set_paused(False)

    def _on_sigint(self, _signum, _frame):
        self._request_stop()

    def _set_paused(self, paused):
        self.paused = paused
        if self.est:
            self.est.pause() if paused else self.est.resume()

    def _poll(self):
        key = self.keys.poll()
        if key == "p" and self.current in ("reorganise", "copy"):
            self._set_paused(not self.paused)
        elif key in ("s", "q", "\x03"):
            self._request_stop()

    def _tick(self, force=False):
        self._poll()
        now = time.monotonic()
        if self.live.active:
            if force or now - self._last_frame >= self.FRAME:
                self._draw()
        elif now - self._last_plain >= self.PLAIN_EVERY:
            self._last_plain = now
            self.t.print(self._plain_status())

    def _wait_while_paused(self):
        while self.paused:
            time.sleep(0.1)
            self._tick(force=True)

    # ------------------------------------------------------------------ scan and compare

    def on_scan(self, result, folder):
        self.scan_files = len(result.files)
        self.scan_bytes = result.bytes
        self.scan_dir = folder
        self._tick()

    def on_check_work(self, files, size):
        self.check_files = files
        self.check_bytes = size
        self.check_done = 0
        self._tick(force=True)

    def on_check_file(self, rel):
        self.check_rel = rel
        self._tick()

    def on_check_bytes(self, count):
        self.check_done += count
        self._tick()

    # ------------------------------------------------------------------ executor events

    def phase(self, name, total=0):
        if self.current:
            self.done.add(self.current)
        self.current = name
        if name == "scan":
            self._scan_started = time.monotonic()
        elif name == "reorganise":
            self.ops_total, self.ops_done = total, 0
        elif name == "copy":
            self.est = progress.Estimator(self.work[0] + self._extra[0], self.work[1] + self._extra[1],
                                          self.prior[0], self.prior[1])
        if not self.live.active and name != "done":
            label = dict(self.phases).get(name, name)
            self.t.print("%s %s" % (self.t.g["arrow"], label))
        self._tick(force=True)

    def step(self):
        self.ops_done += 1
        self._tick()

    def extra_work(self, files, size):
        if self.est:
            self.est.add_work(size, files)
        else:
            self._extra[0] += size
            self._extra[1] += files

    def file_start(self, rel, size):
        self.cur_rel, self.cur_size, self.cur_done = rel, size, 0
        self.est.file_start()
        self._tick()

    def advance(self, count):
        self.cur_done += count
        self.est.add_bytes(count)
        self._tick()
        self._wait_while_paused()
        if self.abort:
            raise Stopped()

    def file_end(self, rel, outcome, size):
        if outcome == "copied":
            if size != self.cur_size:
                self.est.add_work(size - self.cur_size, 0)
            self.est.file_end()
            self.recent.append(("ok", rel, size))
        else:
            self.est.discount(self.cur_size - self.cur_done)
            if outcome == "skipped":
                self.skipped += 1
                self.recent.append(("skip", rel, size))
            else:
                self.recent.append(("same", rel, size))
        self.cur_rel = ""
        self._tick()

    def should_stop(self):
        self._tick()
        self._wait_while_paused()
        return self.stop

    # ------------------------------------------------------------------ drawing

    def _draw(self):
        self._last_frame = time.monotonic()
        self.live.update(self.render())

    def _checklist(self):
        t, g = self.t, self.t.g
        parts = []
        for name, label in self.phases:
            if name in self.done:
                parts.append(t.ok(g["check"]) + " " + t.muted(label))
            elif name == self.current:
                parts.append(t.style(g["arrow"] + " " + label, "lavender", bold=True))
            else:
                parts.append(t.muted(g["ring"] + " " + label))
        return "   ".join(parts)

    def render(self):
        t = self.t
        width = t.width
        inner = width - 4
        body = [self._checklist(), ""]
        if self.current == "scan":
            body += self._scan_lines(inner)
        elif self.current == "compare":
            body += self._compare_lines(inner)
        elif self.current == "reorganise":
            body += self._reorganise_lines(inner)
        elif self.current in ("copy", "save", "done") and self.est:
            body += self._copy_lines(inner)
        elif self.current == "save":
            body += [t.hi("Saving the index to the HDD" + t.g["ell"])]
        title, tone = self.title, "violet"
        if self.paused:
            title, tone = self.title + " " + t.g["sep"] + " paused", "warn"
        elif self.stop:
            title, tone = self.title + " " + t.g["sep"] + " stopping", "warn"
        elif self.current == "done":
            title = self.title + " " + t.g["sep"] + " done"
        lines = t.panel(title, body, width, tone)
        lines.append(self._hint())
        return lines

    def _hint(self):
        t = self.t
        if not self.keys.active or self.current in ("save", "done", None):
            return ""
        key = lambda k, text: t.style(" %s " % k, "lavender", bold=True) + t.muted(text)
        if not self._writing():
            return key("S", "cancel")
        if self.paused:
            return key("P", "continue") + "   " + key("S", "stop") + "   " + t.warn("Paused: the HDD is idle.")
        if self.stop:
            return t.warn("Finishing the current file, then stopping.") + "   " + key("S", "stop right now")
        return key("P", "pause") + "   " + key("S", "stop after this file")

    def _scan_lines(self, inner):
        t = self.t
        lines = [t.hi(self.scan_label + t.g["ell"])]
        counts = "%s  %s  %s" % (plural(self.scan_files, "file"), t.g["sep"], human_bytes(self.scan_bytes))
        if self.expected_files:
            fraction = min(0.99, self.scan_files / self.expected_files)
            lines.append(t.bar(fraction, inner - 8) + " " + t.title("%5.1f%%" % (fraction * 100)))
        lines.append(counts)
        lines.append(t.muted(t.clip_path(show(self.scan_dir) or os.sep, inner)))
        return lines

    def _compare_lines(self, inner):
        t = self.t
        if not self.check_files:
            return [t.hi("Comparing with the index" + t.g["ell"])]
        fraction = self.check_done / self.check_bytes if self.check_bytes else 1.0
        return [
            t.hi("Checking %s whose date changed or that may have moved" % plural(self.check_files, "file")),
            t.bar(fraction, inner - 8) + " " + t.title("%5.1f%%" % (fraction * 100)),
            "%s of %s read from the SSD" % (human_bytes(self.check_done), human_bytes(self.check_bytes)),
            t.muted(t.clip_path(show(self.check_rel), inner)),
        ]

    def _reorganise_lines(self, inner):
        t = self.t
        fraction = self.ops_done / self.ops_total if self.ops_total else 1.0
        return [
            t.hi("Renaming on the HDD: moves and archived deletions"),
            t.bar(fraction, inner - 8) + " " + t.title("%5.1f%%" % (fraction * 100)),
            "%s of %s operations" % (human_count(self.ops_done), human_count(self.ops_total)),
        ]

    def _copy_lines(self, inner):
        t, g, est = self.t, self.t.g, self.est
        now = time.monotonic()
        if not self.paused and now - self._last_sample >= 1.0 and self.current == "copy":
            self._last_sample = now
            self.speeds.append(est.speed())
        eta = est.eta() if self.current == "copy" else 0.0
        if eta is None:
            self._eta = None
        elif self._eta is None or eta == 0:
            self._eta = eta
        else:
            self._eta += 0.2 * (eta - self._eta)

        fraction = 1.0 if self.current in ("save", "done") and not self.stop else est.fraction()
        label = lambda text: t.muted(text + " ")
        lines = [
            t.bar(fraction, inner - 8) + " " + t.title("%5.1f%%" % (fraction * 100)),
            "%s of %s  %s  %s of %s" % (
                human_bytes(est.done_bytes), human_bytes(est.total_bytes), g["sep"],
                human_count(est.done_files), plural(est.total_files, "file")),
            "",
        ]
        if self.cur_rel:
            lines.append(t.hi(t.clip_path(show(self.cur_rel), inner)))
            if self.cur_size >= BIG_FILE:
                share = self.cur_done / self.cur_size
                lines.append(t.bar(share, 24, "violet", "lavender") + "  " + t.muted(
                    "%s of %s" % (human_bytes(self.cur_done), human_bytes(self.cur_size))))
            else:
                lines.append(t.muted(human_bytes(self.cur_size)))
        elif self.current == "save":
            lines += [t.hi("Saving the index and flushing to the HDD" + g["ell"]), ""]
        else:
            lines += ["", ""]
        lines.append("")
        speed = "%s/s" % human_bytes(est.speed()) if self.current == "copy" and not self.paused else "-"
        lines.append(label("Speed") + pad(t.hi(speed), 11) + label("  average")
                     + pad("%s/s" % human_bytes(est.average()), 11) + "  " + t.spark(self.speeds, 24))
        if self._eta is None:
            left, finish = "working it out", "-"
        elif self._eta <= 0:
            left, finish = "-", "-"
        else:
            left = "about " + human_duration(self._eta)
            finish = time.strftime("%H:%M", time.localtime(time.time() + self._eta))
        lines.append(label("Elapsed") + pad(human_duration(est.now()), 9) + label("  remaining")
                     + pad(t.hi(left), 16) + label("  finishes") + finish)
        lines.append("")
        marks = {"ok": t.ok(g["check"]), "skip": t.warn("!"), "same": t.muted(g["check"])}
        tails = {"ok": "", "skip": "  skipped", "same": "  already there"}
        recent = list(self.recent)[::-1]
        for i in range(self.recent.maxlen):
            if i < len(recent):
                kind, rel, size = recent[i]
                tail = human_bytes(size) + tails[kind]
                name = t.clip_path(show(rel), inner - len(tail) - 5)
                lines.append(marks[kind] + " " + t.muted(pad(name, inner - len(tail) - 4) + "  " + tail))
            else:
                lines.append("")
        if self.skipped:
            lines.append(t.warn("%s skipped so far; they are listed at the end" % plural(self.skipped, "file")))
        return lines

    def _plain_status(self):
        if self.current == "scan":
            return "  %s scanned" % plural(self.scan_files, "file")
        if self.current == "copy" and self.est:
            eta = self.est.eta()
            return "  %3.0f%%  %s of %s  %s/s  %s left" % (
                self.est.fraction() * 100, human_bytes(self.est.done_bytes),
                human_bytes(self.est.total_bytes), human_bytes(self.est.speed()),
                human_duration(eta) if eta is not None else "?")
        return "  working"


# =========================================================================== the application

class App:
    def __init__(self, term, cfg_path):
        self.t = term
        self.cfg_path = cfg_path
        self.cfg = config.load(cfg_path)
        self.manifest = None
        self.index_problem = None
        self.source = None
        self.dest = None
        self.source_status = "missing"
        self.dest_status = "missing"

    # ------------------------------------------------------------------ small helpers

    def ask(self, label=""):
        t = self.t
        prompt = t.MARGIN + t.style(t.g["prompt"], "orchid", bold=True) + " " + (label + " " if label else "")
        try:
            return input(prompt).strip()
        except EOFError:
            raise Quit()

    def confirm(self, question, word="yes"):
        """A deliberate confirmation: the user has to type a whole word."""
        self.t.print(question)
        return self.ask('Type "%s" to go ahead:' % word).lower() == word

    def wait(self):
        self.t.print()
        self.ask(self.t.muted("Press Enter to go back"))

    def header(self, title):
        t = self.t
        t.clear()
        t.print("", t.gradient(t.g["diamond"] + " DiskBackup") + "  " + t.muted(t.g["sep"] + "  " + title),
                t.rule(), "")

    def message(self, title, lines, tone="violet"):
        self.t.print(*self.t.panel(title, lines, tone=tone))

    # ------------------------------------------------------------------ drives and index

    def locate_drives(self):
        cfg = self.cfg
        self.source, self.source_status = config.locate(cfg.source, cfg.source_id, cfg.source_sub)
        self.dest, self.dest_status = config.locate(cfg.dest, cfg.dest_id, cfg.dest_sub)
        moved = False
        if self.source_status == "moved":
            cfg.source, moved = self.source, True
        if self.dest_status == "moved":
            cfg.dest, moved = self.dest, True
            self.manifest = None
        if moved:
            config.save(cfg, self.cfg_path)

    def load_index(self):
        """Read the index from the HDD once and keep it for the session."""
        if self.manifest is not None or not self.dest:
            return
        try:
            self.manifest = Manifest.load(self.dest)
            self.index_problem = None
        except ManifestError as exc:
            self.index_problem = str(exc)

    def drive_problem(self, which):
        status = self.source_status if which == "source" else self.dest_status
        path = self.cfg.source if which == "source" else self.cfg.dest
        name = "SSD" if which == "source" else "backup drive"
        if status == "missing":
            return "The %s is not connected (%s)." % (name, path)
        if status == "different":
            return "%s is now a different drive from the one that was set up." % path
        return None

    def ready(self):
        """Both drives present and the index readable; explains itself if not."""
        self.locate_drives()
        self.load_index()
        problems = [p for p in (self.drive_problem("source"), self.drive_problem("dest")) if p]
        if not problems and self.index_problem:
            problems = [self.index_problem, 'Use "Rebuild index" from the menu to recreate it from the HDD.']
        if not problems and self.manifest is None:
            problems = ["The backup index could not be read."]
        if problems:
            self.message("Cannot start", [self.t.err(p) for p in problems], "err")
            self.wait()
            return False
        return True

    # ------------------------------------------------------------------ home

    def run(self):
        if not self.cfg.ready:
            self.setup()
        while True:
            self.home()

    def banner(self):
        t = self.t
        g = t.g
        name = t.gradient(g["diamond"] + " DiskBackup")
        tag = t.muted("only what changed, SSD %s HDD   v%s" % (g["to"], __version__))
        inner = t.width - 4
        gap = max(2, inner - cell_width(name) - cell_width(tag))
        edge = t.accent
        t.print(edge(g["tl"] + g["h"] * (t.width - 2) + g["tr"]),
                edge(g["v"]) + " " + pad(clip_ansi(name + " " * gap + tag, inner), inner) + " " + edge(g["v"]),
                edge(g["bl"] + g["h"] * (t.width - 2) + g["br"]), "")

    def drive_panel(self, title, path, shown, status, width, free_matters):
        t = self.t
        inner = width - 4
        lines = [t.title(t.clip_path(clean(shown), inner))]
        if status in ("missing", "different") or not path:
            lines += [t.err("Not connected" if status == "missing" else "A different drive is here"), ""]
            return t.panel(title, lines, width, "err")
        vol = osutil.volume_of(path)
        facts = [f for f in ((vol.fstype if vol else ""), (vol.label if vol else ""), (vol.id if vol else "")) if f]
        lines.append(t.muted((" %s " % t.g["sep"]).join(clean(f) for f in facts) or "local folder"))
        usage = osutil.disk_usage(path)
        if usage:
            text = ("%s free of %s" if free_matters else "%s used of %s") % (
                human_bytes(usage.free if free_matters else usage.used), human_bytes(usage.total))
            bar_width = max(6, inner - len(text) - 2)
            lines.append(t.bar(usage.used / usage.total if usage.total else 0, bar_width) + "  " + text)
        else:
            lines.append("")
        return t.panel(title, lines, width)

    def home(self):
        t = self.t
        self.locate_drives()
        self.load_index()
        t.clear()
        t.print()
        self.banner()

        wide = t.width >= 76
        width = (t.width - 2) // 2 if wide else t.width
        left = self.drive_panel("Source (SSD)", self.source, self.cfg.source, self.source_status, width, False)
        right = self.drive_panel("Backup (HDD)", self.dest, self.cfg.dest, self.dest_status, width, True)
        t.print(*(t.columns_of(left, right) if wide else left + right))
        t.print()

        label = lambda text: t.muted(pad(text, 13))
        last = self.manifest.last_run if self.manifest else None
        if self.index_problem:
            t.print(t.err(self.index_problem))
        elif last:
            state = t.warn("  (stopped early)") if last.get("stopped") else ""
            t.print(label("Last backup") + t.title(when(last["ts"])) + t.muted(
                "  %s  %s" % (t.g["sep"], human_ago(last["ts"]))) + state)
            t.print(label("") + "%s copied %s %s written %s took %s" % (
                plural(last["copied_files"], "file"), t.g["sep"], human_bytes(last["copied_bytes"]),
                t.g["sep"], human_duration(last["seconds"])))
            t.print(label("Protected") + "%s %s %s" % (
                plural(len(self.manifest.files), "file"), t.g["sep"], human_bytes(self.manifest.total_bytes)))
        elif self.manifest is not None:
            t.print(label("Last backup") + t.hi("none yet") + t.muted("  choose 1 to make the first one"))
        t.print()

        items = (("1", "Back up now", "scan, preview, then copy only what changed"),
                 ("2", "Preview changes", "see what a backup would do; writes nothing"),
                 ("3", "History", "past runs"),
                 ("4", "Archived deletions", "files removed from the SSD, kept on the HDD by date"),
                 ("5", "Settings", "drives and exclusions"),
                 ("6", "Rebuild index", "re-read the HDD after changing it by hand"),
                 ("Q", "Quit", ""))
        for key, name, hint in items:
            t.print(t.style(" %s " % key, "orchid", bold=True) + " " + pad(name, 20) + t.muted(hint))
        t.print()
        choice = self.ask().lower()
        actions = {"1": self.backup, "2": lambda: self.backup(preview_only=True), "3": self.history,
                   "4": self.archives, "5": self.settings, "6": self.rebuild_index}
        if choice in ("q", "quit", "exit"):
            raise Quit()
        if choice in actions:
            actions[choice]()

    # ------------------------------------------------------------------ setup and settings

    def pick_folder(self, question, example):
        """Let the user pick a mounted drive from a list, or type a path."""
        t = self.t
        volumes = osutil.list_volumes()
        t.print(t.title(question), "")
        rows = []
        for i, vol in enumerate(volumes, 1):
            usage = osutil.disk_usage(vol.mount)
            rows.append([t.style(" %d " % i, "orchid", bold=True), clean(vol.mount), clean(vol.label),
                         vol.fstype, human_bytes(usage.total) if usage else "",
                         t.muted(human_bytes(usage.free) + " free") if usage else ""])
        t.print(*t.table(rows, "<<<<><"))
        t.print("", t.muted("Type a number%s a full path such as %s. Leave empty to cancel." % (
            ", or" if volumes else " is not possible here; type", example)))
        while True:
            answer = self.ask()
            if not answer:
                return None
            if answer.isdigit() and 1 <= int(answer) <= len(volumes):
                return volumes[int(answer) - 1].mount
            path = os.path.abspath(os.path.expanduser(answer.strip('"')))
            if os.path.isdir(path):
                return path
            t.print(t.err("That folder does not exist."))

    def choose_source(self):
        path = self.pick_folder("Which drive do you want to back up (your SSD)?",
                                "D:\\" if osutil.IS_WINDOWS else "/media/you/SSD")
        if not path:
            return False
        if self.cfg.dest and (osutil.is_inside(self.cfg.dest, path) or osutil.is_inside(path, self.cfg.dest)):
            self.t.print(self.t.err("The backup folder and the source cannot be inside one another."))
            return False
        self.cfg.set_source(path)
        return True

    def choose_dest(self):
        t = self.t
        drive = self.pick_folder("Which drive should hold the backup (your HDD)?",
                                 "E:\\" if osutil.IS_WINDOWS else "/media/you/HDD")
        if not drive:
            return False
        t.print("", t.muted("The backup goes in its own folder there, holding current%s and the dated folders." % os.sep))
        name = self.ask("Folder name [DiskBackup]:") or "DiskBackup"
        path = os.path.abspath(os.path.join(drive, name.strip('"')))
        source = self.cfg.source
        if source and (osutil.is_inside(path, source) or osutil.is_inside(source, path)):
            t.print(t.err("The backup folder cannot be inside the drive being backed up."))
            return False
        try:
            os.makedirs(path, exist_ok=True)
        except OSError as exc:
            t.print(t.err("Cannot create %s: %s" % (path, scanner.reason(exc))))
            return False
        src_vol, dst_vol = osutil.volume_of(source) if source else None, osutil.volume_of(path)
        if src_vol and dst_vol and src_vol.mount == dst_vol.mount:
            t.print(t.warn("Note: that is the same drive as the source, so it will not survive a drive failure."))
        self.cfg.set_dest(path)
        self.manifest = None
        self.index_problem = None
        return True

    def setup(self):
        t = self.t
        self.header("First-time setup")
        t.print("DiskBackup keeps a copy of one drive on another and, after the first run,",
                "only ever writes the files that changed.", "")
        while not self.cfg.source:
            if not self.choose_source():
                raise Quit()
        t.print()
        while not self.cfg.dest:
            if not self.choose_dest():
                raise Quit()
        config.save(self.cfg, self.cfg_path)
        t.print("", t.ok(t.g["check"] + " Saved."), t.muted("Settings file: " + self.cfg_path))
        self.wait()

    def settings(self):
        t = self.t
        while True:
            self.header("Settings")
            rows = [[t.style(" 1 ", "orchid", bold=True), "Source (SSD)", clean(self.cfg.source)],
                    [t.style(" 2 ", "orchid", bold=True), "Backup folder (HDD)", clean(self.cfg.dest)],
                    [t.style(" 3 ", "orchid", bold=True), "Exclusions", plural(len(self.cfg.excludes), "pattern")]]
            t.print(*t.table(rows, "<<<"))
            t.print("", t.muted("Settings file: " + self.cfg_path), "",
                    t.muted("Choose a number, or press Enter to go back."))
            choice = self.ask()
            if choice == "1":
                t.print()
                if self.choose_source():
                    config.save(self.cfg, self.cfg_path)
            elif choice == "2":
                t.print()
                if self.choose_dest():
                    config.save(self.cfg, self.cfg_path)
            elif choice == "3":
                self.exclusions()
            else:
                return

    def exclusions(self):
        t = self.t
        while True:
            self.header("Exclusions")
            t.print("Files and folders matching these patterns are not backed up.",
                    t.muted("A plain name matches anywhere (*.tmp, node_modules). With a slash it is a"),
                    t.muted("path from the top of the drive (Games/Cache). Something already backed up"),
                    t.muted("that you exclude later is moved to the dated archive on the next run."), "")
            for i, pattern in enumerate(self.cfg.excludes, 1):
                t.print(t.style(" %d " % i, "orchid", bold=True) + " " + clean(pattern))
            t.print("", t.muted("Type a new pattern to add it, a number to remove one, or Enter to go back."))
            answer = self.ask()
            if not answer:
                return
            if answer.isdigit():
                if 1 <= int(answer) <= len(self.cfg.excludes):
                    del self.cfg.excludes[int(answer) - 1]
            elif answer not in self.cfg.excludes:
                self.cfg.excludes.append(answer)
            config.save(self.cfg, self.cfg_path)

    # ------------------------------------------------------------------ backup

    def backup(self, preview_only=False):
        t = self.t
        title = "Preview changes" if preview_only else "Back up now"
        self.header(title)
        if not self.ready():
            return
        manifest = self.manifest
        if not manifest.loaded and not manifest.files and self._has_existing_copy():
            self.message("Existing files found", [
                "The backup folder already holds files but has no index yet.",
                'Run "Rebuild index" first so they are recognised instead of copied again.'], "warn")
            self.wait()
            return

        view = RunView(t, "Looking for changes")
        view.expected_files = len(manifest.files)
        try:
            with view:
                scan, plan = self._scan_and_plan(view, manifest)
        except (Cancelled, KeyboardInterrupt):
            t.print("", t.warn("Cancelled. Nothing was written."))
            self.wait()
            return
        except OSError as exc:
            t.print("", t.err("The source could not be read: %s" % scanner.reason(exc)))
            self.wait()
            return

        self.header(title)
        if not scan.files and manifest.files:
            self.message("Stopped for safety", [
                t.err("The source is empty, but the backup holds %s." % plural(len(manifest.files), "file")),
                "Is the right drive connected? Nothing was changed."], "err")
            self.wait()
            return
        if plan.is_empty:
            self.message("Up to date", [
                t.ok(t.g["check"] + " Nothing has changed since the last backup."),
                "%s %s %s checked. Nothing was written to the HDD." % (
                    plural(plan.scan_files, "file"), t.g["sep"], human_bytes(plan.scan_bytes))])
            self._notes(plan)
            self.wait()
            return

        t.print(*self.preview_lines(plan, manifest))
        self._notes(plan)
        if not plan.touches_hdd:
            t.print("", t.muted("%s had a new date but identical contents, so nothing needs copying." %
                                plural(len(plan.retimed), "file")))
            if not preview_only:
                self._apply_index_only(plan, manifest)
            self.wait()
            return
        if not self._guards(plan, manifest, preview_only):
            return

        while True:
            t.print()
            if preview_only:
                t.print(t.muted("Enter to go back   V to list every file"))
            else:
                t.print(t.style(" Enter ", "orchid", bold=True) + "start   "
                        + t.style(" V ", "orchid", bold=True) + "list every file   "
                        + t.style(" Q ", "orchid", bold=True) + "cancel")
            answer = self.ask().lower()
            if answer == "v":
                self.details(plan)
                self.header(title)
                t.print(*self.preview_lines(plan, manifest))
                continue
            if preview_only or answer in ("q", "n", "no", "cancel"):
                return
            if answer in ("", "y", "yes", "s", "start"):
                break
        self._execute(plan, manifest)

    def _has_existing_copy(self):
        try:
            with os.scandir(os.path.join(osutil.fs_path(self.dest), CURRENT)) as entries:
                return any(True for _ in entries)
        except OSError:
            return False

    def _scan_and_plan(self, view, manifest):
        src_root = osutil.fs_path(self.source)
        buf = bytearray(scanner.CHUNK)
        view.phase("scan")
        scan = scanner.scan(self.source, self.cfg.excludes, on_progress=view.on_scan)
        view.phase("compare")

        def fingerprint(rel):
            view.on_check_file(rel)
            try:
                return scanner.fingerprint(osutil.join(src_root, rel), view.on_check_bytes, buf)
            except OSError:
                return None

        plan = planner.build_plan(scan, manifest, fingerprint, view.on_check_work)
        view.phase("done")
        return scan, plan

    def _notes(self, plan):
        """Things the user should know that are not part of the plan itself."""
        t = self.t
        if plan.unreadable:
            t.print("", t.warn("%s could not be read on the SSD and will be left as they are in the backup:" %
                               plural(len(plan.unreadable), "item")))
            for rel, why in sorted(plan.unreadable.items())[:5]:
                t.print(t.muted("  %s (%s)" % (t.clip_path(show(rel), t.width - 30), why)))
            if len(plan.unreadable) > 5:
                t.print(t.muted("  and %d more" % (len(plan.unreadable) - 5)))

    def _apply_index_only(self, plan, manifest):
        for rel, mtime_ns in plan.retimed.items():
            if rel in manifest.files:
                manifest.files[rel][1] = mtime_ns
        try:
            manifest.save(self.dest)
            self.t.print(self.t.muted("The index was updated; no files were touched."))
        except OSError as exc:
            self.t.print(self.t.err("The index could not be saved: %s" % scanner.reason(exc)))

    def _guards(self, plan, manifest, preview_only):
        """Checks that can refuse a run. True means carry on."""
        t = self.t
        usage = osutil.disk_usage(self.dest)
        if usage and plan.space_needed > usage.free:
            self.message("Not enough room on the HDD", [
                t.err("This backup needs %s but only %s is free." % (
                    human_bytes(plan.space_needed), human_bytes(usage.free))),
                'Free some space, for example under "Archived deletions".'], "err")
            self.wait()
            return False
        if preview_only:
            return True
        vol = osutil.volume_of(self.source)
        if manifest.source_id and vol and vol.id and not osutil.same_id(vol.id, manifest.source_id):
            t.print()
            if not self.confirm(t.warn("This backup was made from a different drive (ID %s, now %s)." % (
                    manifest.source_id, vol.id))):
                return False
        if (plan.deleted_files >= MASS_DELETE_MIN
                and plan.deleted_files > MASS_DELETE_SHARE * plan.index_files):
            t.print()
            share = 100.0 * plan.deleted_files / plan.index_files
            if not self.confirm(t.warn(
                    "%s (%.0f%% of the backup) are gone from the SSD and would be moved to the archive." % (
                        plural(plan.deleted_files, "file"), share))):
                return False
        return True

    def preview_lines(self, plan, manifest):
        t, g = self.t, self.t.g
        today = time.strftime("%Y-%m-%d")
        dot = lambda tone: t.style(g["dot"], tone)
        rows = [
            ["", "", t.muted("Files"), t.muted("Size"), ""],
            [dot("ok"), "New", human_count(plan.new_files), human_bytes(plan.new_bytes),
             t.muted("copied to the HDD")],
            [dot("orchid"), "Changed", human_count(plan.changed_files), human_bytes(plan.changed_bytes),
             t.muted("overwritten on the HDD")],
            [dot("violet"), "Moved", human_count(plan.moved_files), human_bytes(plan.moved_bytes),
             t.muted("renamed on the HDD, not recopied")],
            [dot("warn"), "Deleted", human_count(plan.deleted_files), human_bytes(plan.deleted_bytes),
             t.muted("kept in %s%s" % (today, os.sep))],
            [t.muted(g["ring"]), "Unchanged", human_count(plan.unchanged_files),
             human_bytes(plan.unchanged_bytes), t.muted("not touched")],
        ]
        last = manifest.last_run
        title = "Changes since %s" % when(last["ts"]) if last else "First backup"
        body = t.table(rows, "<<>><", gap=2)

        body.append("")
        if plan.scan_bytes:
            share = plan.copy_bytes / plan.scan_bytes
            body.append("Writing %s instead of %s" % (
                t.title(human_bytes(plan.copy_bytes)), human_bytes(plan.scan_bytes)))
            body.append(t.bar(share, 30) + "  " + t.muted("%.1f%% of a full copy" % (100 * share)))
        if plan.checked_files:
            body.append(t.muted("%s read on the SSD to check for real changes and moves (%s)" % (
                plural(plan.checked_files, "file"), human_bytes(plan.checked_bytes))))

        usage = osutil.disk_usage(self.dest)
        facts = []
        if usage:
            facts.append(t.muted("HDD free ") + "%s %s %s" % (
                human_bytes(usage.free), g["to"], human_bytes(max(0, usage.free - plan.space_needed))))
        guess = progress.estimate(plan.copy_bytes, plan.copy_files, manifest.rate, manifest.overhead)
        facts.append(t.muted("Estimated time ") + (
            "about " + human_duration(guess) if guess is not None else "shown once copying starts"))
        body += ["", "     ".join(facts)]
        lines = t.panel(title, body)

        if plan.copies:
            folders = Counter()
            for op in plan.copies:
                parts = op.rel.split("/")[:-1][:2]
                folders["/".join(parts) if parts else ""] += op.size
            left = [t.title("Where the changes are")]
            for folder, size in folders.most_common(5):
                name = show(folder) + os.sep if folder else "(top folder)"
                left.append(pad(t.clip_path(name, 30), 31) + pad(human_bytes(size), 9, ">"))
            right = [t.title("Largest files to copy")]
            for op in sorted(plan.copies, key=lambda op: -op.size)[:5]:
                right.append(pad(t.clip_path(show(op.rel), 30), 31) + pad(human_bytes(op.size), 9, ">"))
            lines.append("")
            lines += t.columns_of(left, right, gap=4) if t.width >= 86 else left + [""] + right
        return lines

    def details(self, plan):
        """Every operation in the plan, a screenful at a time."""
        t, g = self.t, self.t.g
        today = time.strftime("%Y-%m-%d")
        lines = []

        def section(name, items):
            if items:
                lines.append(t.title("%s (%s)" % (name, human_count(len(items)))))
                lines.extend(items)
                lines.append("")

        size_of = lambda op: t.muted("  " + human_bytes(op.size))
        section("Copy to the HDD", [t.ok("+ ") + show(op.rel) + size_of(op)
                                    for op in plan.copies if op.kind == "new"])
        section("Overwrite on the HDD", [t.style("~ ", "orchid") + show(op.rel) + size_of(op)
                                         for op in plan.copies if op.kind == "changed"])
        moves = ["%s%s %s %s%s  %s" % (show(dm.old), os.sep, g["to"], show(dm.new), os.sep,
                                       t.muted("(%s, one rename)" % plural(len(dm.files), "file")))
                 for dm in plan.dir_moves]
        moves += ["%s %s %s" % (show(mv.old), g["to"], show(mv.new)) for mv in plan.file_moves]
        section("Rename on the HDD", moves)
        gone = ["%s %s%s  %s" % (t.warn("-"), show(ad.rel), os.sep,
                                 t.muted("(%s, one rename)" % plural(len(ad.files), "file")))
                for ad in plan.archive_dirs]
        gone += [t.warn("- ") + show(rel) + t.muted("  " + human_bytes(size)) for rel, size in plan.archive_files]
        section("Move to %s%s" % (today, os.sep), gone)

        page = max(5, t.rows - 6)
        for start in range(0, len(lines), page):
            self.header("Every file in this backup")
            t.print(*lines[start:start + page])
            if start + page >= len(lines):
                break
            shown = min(len(lines), start + page)
            if self.ask(t.muted("%d of %d lines. Enter for more, Q to stop" % (shown, len(lines)))).lower() == "q":
                return
        self.wait()

    def _execute(self, plan, manifest):
        t = self.t
        self.header("Back up now")
        started = time.time()
        clock = time.monotonic()
        view = RunView(t, "Backing up", done=("scan", "compare"))
        view.work = (plan.copy_bytes, plan.copy_files)
        view.prior = (manifest.rate, manifest.overhead)
        executor = Executor(self.source, self.dest, manifest, plan, view)
        failure = save_problem = log_path = None
        with view:
            try:
                result = executor.run()
            except BackupError as exc:
                failure = str(exc)
                result = executor.res
                result.stopped = True
            finally:
                # Whatever happened, the index must describe what is now on the HDD.
                view.phase("save")
                seconds = time.monotonic() - clock
                vol = osutil.volume_of(self.source)
                manifest.source_id = (vol.id if vol else None) or manifest.source_id
                if view.est:
                    manifest.rate = view.est.measured_rate or manifest.rate
                    manifest.overhead = view.est.overhead if view.est.done_files else manifest.overhead
                manifest.add_history(history_entry(plan, executor.res, started, seconds))
                try:
                    manifest.save(self.dest)
                    log_path = write_log(self.dest, started, log_lines(
                        self.source, self.dest, plan, executor.res, started, seconds))
                    osutil.flush_disks()
                except OSError as exc:
                    save_problem = scanner.reason(exc)
                view.phase("done")
        self.summary(plan, result, seconds, view, failure, save_problem, log_path)

    def summary(self, plan, result, seconds, view, failure, save_problem, log_path):
        t, g = self.t, self.t.g
        rows = [
            [t.ok(g["dot"]), "Copied", plural(result.copied_files, "file"), human_bytes(result.copied_bytes)],
            [t.style(g["dot"], "violet"), "Moved", plural(result.moved_files, "file"),
             t.muted("renamed, nothing rewritten")],
            [t.warn(g["dot"]), "Archived", plural(result.archived_files, "file"),
             human_bytes(result.archived_bytes) + t.muted("  in %s%s" % (time.strftime("%Y-%m-%d"), os.sep))],
            [t.muted(g["ring"]), "Untouched", plural(plan.unchanged_files, "file"),
             human_bytes(plan.unchanged_bytes)],
        ]
        if result.resumed_files:
            rows.insert(1, [t.muted(g["dot"]), "Already there", plural(result.resumed_files, "file"),
                            t.muted("from an interrupted run")])
        body = t.table(rows, "<<><")
        body.append("")
        if result.stopped:
            body.append("Wrote %s before stopping" % t.title(human_bytes(result.copied_bytes)))
        elif plan.scan_bytes:
            saved = max(0, plan.scan_bytes - result.copied_bytes)
            body.append("Wrote %s; a full copy would have been %s (%s avoided)" % (
                t.title(human_bytes(result.copied_bytes)), human_bytes(plan.scan_bytes), human_bytes(saved)))
        pace = ""
        if view.est and view.est.done_bytes >= scanner.CHUNK and view.est.now() > 0:
            pace = " at %s/s on average" % human_bytes(view.est.average())
        body.append("Took %s%s" % (t.title(human_duration(seconds)), pace))

        self.header("Back up now")
        if failure:
            self.message("Backup stopped", [t.err(failure), ""] + body, "err")
        elif result.stopped:
            left = result.remaining_files
            self.message("Stopped early", [t.warn("%s still to copy. Run the backup again to finish;" %
                                                  plural(left, "file")),
                                           t.warn("what is already done will not be copied twice."), ""] + body,
                         "warn")
        else:
            self.message("Backup complete", [t.ok(g["check"] + " The HDD is up to date."), ""] + body)

        if save_problem:
            t.print(t.err("The index could not be saved: %s" % save_problem),
                    t.err('Run "Rebuild index" before the next backup.'))
        if result.skipped:
            t.print("", t.warn("%s skipped; they will be tried again next time:" %
                               plural(len(result.skipped), "file")))
            for rel, why in result.skipped[:12]:
                t.print(t.muted("  %s (%s)" % (t.clip_path(show(rel), t.width - 34), why)))
            if len(result.skipped) > 12:
                t.print(t.muted("  and %d more, listed in the log" % (len(result.skipped) - 12)))
        for text in result.warnings[:5]:
            t.print(t.warn(clean(text)))
        if log_path:
            t.print("", t.muted("Log: " + log_path.replace("\\\\?\\", "")))
        if osutil.IS_WINDOWS and not save_problem:
            t.print(t.muted('Use "Safely remove hardware" before unplugging the HDD.'))
        self.wait()

    # ------------------------------------------------------------------ history

    @staticmethod
    def _avoided(run):
        """Bytes a full copy would have written that this run did not have to."""
        if run.get("stopped"):
            return 0   # an unfinished run has not saved anything yet
        return max(0, run.get("total_bytes", 0) - run.get("copied_bytes", 0))

    def history(self):
        t = self.t
        self.header("History")
        if not self.ready():
            return
        runs = self.manifest.history[::-1][:20]
        if not runs:
            t.print(t.muted("No backups have been made yet."))
            self.wait()
            return
        rows = [[t.muted(h) for h in ("When", "Took", "Copied", "Written", "Moved", "Archived", "Avoided", "")]]
        for run in runs:
            avoided = self._avoided(run)
            state = t.warn("stopped") if run.get("stopped") else (
                t.warn("%d skipped" % run["skipped"]) if run.get("skipped") else t.ok(t.g["check"]))
            rows.append([when(run["ts"]), human_duration(run.get("seconds", 0)),
                         human_count(run.get("copied_files", 0)), t.title(human_bytes(run.get("copied_bytes", 0))),
                         human_count(run.get("moved_files", 0)), human_count(run.get("archived_files", 0)),
                         "-" if run.get("stopped") else human_bytes(avoided), state])
        t.print(*t.table(rows, "<>>>>>><"))
        total = sum(run.get("copied_bytes", 0) for run in self.manifest.history)
        avoided = sum(self._avoided(run) for run in self.manifest.history)
        t.print("", "Across %s: %s written to the HDD, %s of rewriting avoided." % (
            plural(len(self.manifest.history), "run"), t.title(human_bytes(total)), t.title(human_bytes(avoided))))
        self.wait()

    # ------------------------------------------------------------------ archived deletions

    def archives(self):
        t = self.t
        while True:
            self.header("Archived deletions")
            if not self.ready():
                return
            archives = self.manifest.archives
            if not archives:
                t.print(t.muted("Nothing has been archived. Files deleted from the SSD will appear here,"),
                        t.muted("in a folder named after the date of the backup that noticed."))
                self.wait()
                return
            dates = sorted(archives)
            rows = [["", t.muted("Folder"), t.muted("Files"), t.muted("Size")]]
            for i, date in enumerate(dates, 1):
                rows.append([t.style(" %d " % i, "orchid", bold=True), date + os.sep,
                             human_count(archives[date]["files"]), human_bytes(archives[date]["bytes"])])
            t.print(*t.table(rows, "<<>>"))
            total = sum(entry["bytes"] for entry in archives.values())
            t.print("", "%s in %s, inside %s" % (t.title(human_bytes(total)),
                                                 plural(len(dates), "folder"), clean(self.dest)),
                    "", t.muted('Type a number to delete that folder for good, "all" for every one,'),
                    t.muted("or press Enter to go back."))
            answer = self.ask().lower()
            if not answer:
                return
            if answer == "all":
                chosen = dates
            elif answer.isdigit() and 1 <= int(answer) <= len(dates):
                chosen = [dates[int(answer) - 1]]
            else:
                continue
            size = sum(archives[date]["bytes"] for date in chosen)
            if not self.confirm(t.warn("Permanently delete %s (%s)? This cannot be undone." % (
                    ", ".join(chosen), human_bytes(size))), "delete"):
                continue
            for date in chosen:
                try:
                    osutil.remove_tree(os.path.join(osutil.fs_path(self.dest), date))
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    t.print(t.err("Could not delete %s: %s" % (date, scanner.reason(exc))))
                    self.wait()
                    continue
                del archives[date]
            try:
                self.manifest.save(self.dest)
            except OSError as exc:
                t.print(t.err("The index could not be saved: %s" % scanner.reason(exc)))
                self.wait()

    # ------------------------------------------------------------------ rebuild

    def rebuild_index(self):
        t = self.t
        self.header("Rebuild index")
        self.locate_drives()
        problems = [p for p in (self.drive_problem("source"), self.drive_problem("dest")) if p]
        if problems:
            self.message("Cannot start", [t.err(p) for p in problems], "err")
            self.wait()
            return
        t.print("This reads the folder listing of the whole HDD backup and matches it against",
                "the SSD. Files with the same size and date are taken as backed up. Files with",
                "the same size but a different date are read on both drives and compared.", "",
                t.muted("It is only needed for a backup made some other way, after changing the"),
                t.muted("HDD by hand, or if the index is damaged. No files are copied."), "")
        if self.ask("Start? [y/N]").lower() not in ("y", "yes"):
            return
        self.header("Rebuild index")
        view = RunView(t, "Rebuilding the index", REBUILD_PHASES)
        view.scan_label = "Reading both drives"
        try:
            with view:
                view.phase("scan")

                def compare_starts(files, size):
                    view.phase("compare")
                    view.on_check_work(files, size)

                manifest, stats = rebuild.rebuild(self.source, self.dest, self.cfg.excludes, self.manifest,
                                                  view.on_scan, compare_starts, view.on_check_bytes)
                view.phase("save")
                manifest.save(self.dest)
                view.phase("done")
        except (Cancelled, KeyboardInterrupt):
            t.print("", t.warn("Cancelled. The index was not changed."))
            self.wait()
            return
        except OSError as exc:
            t.print("", t.err("Could not finish: %s" % scanner.reason(exc)))
            self.wait()
            return
        self.manifest = manifest
        self.index_problem = None
        rows = [["Recognised as backed up", human_count(stats.matched + stats.verified)],
                ["  of which compared byte for byte", human_count(stats.verified)],
                ["Different on the HDD (next backup recopies)", human_count(stats.different)],
                ["Only on the HDD (next backup archives)", human_count(stats.only_hdd)],
                ["Not backed up yet", human_count(stats.only_source)],
                ["Unfinished temporary files removed", human_count(stats.leftovers)]]
        t.print()
        self.message("Index rebuilt", t.table(rows, "<>"))
        self.wait()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="diskbackup", description="Back up one drive to another, writing only what changed.")
    parser.add_argument("--config", metavar="FILE", help="settings file to use (default: %s)" % config.default_path())
    parser.add_argument("--ascii", action="store_true", help="draw with plain ASCII characters")
    parser.add_argument("--no-color", action="store_true", help="do not use colour")
    parser.add_argument("--version", action="version", version="DiskBackup " + __version__)
    args = parser.parse_args(argv)

    term = Term(depth=0 if args.no_color else None, charset="ascii" if args.ascii else None)
    app = App(term, args.config or config.default_path())
    try:
        app.run()
    except (Quit, KeyboardInterrupt):
        term.print("", term.muted("Goodbye."))
    return 0
