"""Apply a Plan to the HDD.

Renames come first (moves, then archiving deletions), then the copies, folder
by folder. The index is updated in memory as each step succeeds, so whatever
happens it describes what is really on the HDD when it is saved.
"""

import errno
import hashlib
import os
import time

from . import osutil
from .manifest import CURRENT
from .planner import CopyOp, parent, under
from .scanner import CHUNK, reason

TMP_SUFFIX = ".dbktmp"
HEAL_TOLERANCE_NS = 2_000_000_000   # coarsest timestamp any common filesystem keeps
LOG_DETAIL_LIMIT = 50_000


class Stopped(Exception):
    """The user asked to stop."""


class BackupError(Exception):
    """Something that makes carrying on pointless: drive full or gone."""


class _ReadError(Exception):
    pass


class _WriteError(Exception):
    pass


class Events:
    """What the executor reports while it works. The UI subclasses this."""

    def phase(self, name, total=0):
        pass

    def step(self):
        """One reorganise operation finished."""

    def file_start(self, rel, size):
        pass

    def advance(self, count):
        """`count` more bytes copied. May block (pause) or raise Stopped."""

    def file_end(self, rel, outcome, size):
        """outcome is "copied", "resumed" (already on the HDD) or "skipped"."""

    def extra_work(self, files, size):
        """A rename could not be done, so this much more has to be copied."""

    def should_stop(self):
        return False


class Result:
    def __init__(self):
        self.copied_files = 0
        self.copied_bytes = 0
        self.resumed_files = 0
        self.moved_files = 0
        self.moved_dirs = 0
        self.archived_files = 0
        self.archived_bytes = 0
        self.skipped = []     # [(index path, reason)]
        self.warnings = []
        self.stopped = False
        self.remaining_files = 0
        self.details = []     # one line per operation, for the log
        self.details_dropped = 0

    def log(self, line):
        if len(self.details) < LOG_DETAIL_LIMIT:
            self.details.append(line)
        else:
            self.details_dropped += 1


class Executor:
    def __init__(self, source, dest, manifest, plan, events=None, date=None):
        self.src = osutil.fs_path(source)
        self.dest = osutil.fs_path(dest)
        self.cur = os.path.join(self.dest, CURRENT)
        self.date = date or time.strftime("%Y-%m-%d")
        self.arch = os.path.join(self.dest, self.date)
        self.m = manifest
        self.plan = plan
        self.ev = events or Events()
        self.res = Result()
        self._buf = bytearray(CHUNK)
        self._view = memoryview(self._buf)
        self._arch_dirs = set()
        self._late_archives = []
        self._late_copies = []

    # ------------------------------------------------------------------ run

    def run(self):
        plan = self.plan
        self.m.dirty = True
        try:
            os.makedirs(self.cur, exist_ok=True)
        except OSError as exc:
            raise BackupError("Cannot create the backup folder: %s" % reason(exc))
        for rel, mtime_ns in plan.retimed.items():
            if rel in self.m.files:
                self.m.files[rel][1] = mtime_ns
        try:
            self.ev.phase("reorganise", plan.reorganise_ops)
            for group, action in ((plan.dir_moves, self._move_dir),
                                  (plan.file_moves, self._move_file),
                                  (plan.archive_dirs, self._archive_dir),
                                  (plan.archive_files, self._archive_file),
                                  (plan.rmdirs, self._rmdir),
                                  (plan.mkdirs, self._mkdir)):
                for item in group:
                    self._gate()
                    action(item)
                    self.ev.step()
            for item in self._late_archives:
                self._archive_file(item)

            copies = plan.copies
            if self._late_copies:
                copies = sorted(copies + self._late_copies, key=lambda op: (parent(op.rel), op.rel))
            self.ev.phase("copy", len(copies))
            for done, op in enumerate(copies):
                self.res.remaining_files = len(copies) - done
                self._gate()
                self._copy(op)
            self.res.remaining_files = 0
        except Stopped:
            self.res.stopped = True
        return self.res

    def _gate(self):
        if self.ev.should_stop():
            raise Stopped()

    # ------------------------------------------------------------------ folders

    def _ensure_dir(self, rel):
        if not rel or rel in self.m.dirs:
            return
        os.makedirs(osutil.join(self.cur, rel), exist_ok=True)
        while rel and rel not in self.m.dirs:
            self.m.dirs.add(rel)
            rel = parent(rel)

    def _mkdir(self, rel):
        try:
            self._ensure_dir(rel)
        except OSError as exc:
            self.res.warnings.append("Could not create folder %s: %s" % (rel, reason(exc)))

    def _rmdir(self, rel):
        self.m.dirs.discard(rel)
        try:
            os.rmdir(osutil.join(self.cur, rel))
        except FileNotFoundError:
            pass
        except OSError:
            self.res.warnings.append("Left folder %s on the HDD: it holds files the index does not know" % rel)

    def _prune(self, folder):
        """Remove a folder tree that should now be empty, as far as it really is."""
        for rel in sorted((d for d in self.m.dirs if under(d, folder)), key=lambda d: -d.count("/")):
            try:
                os.rmdir(osutil.join(self.cur, rel))
            except FileNotFoundError:
                pass
            except OSError:
                continue   # still holds something; a later run tidies it
            self.m.dirs.discard(rel)

    def _forget_dirs(self, folder):
        for rel in [d for d in self.m.dirs if under(d, folder)]:
            self.m.dirs.discard(rel)

    # ------------------------------------------------------------------ moves

    def _move_file(self, mv):
        src = osutil.join(self.cur, mv.old)
        dst = osutil.join(self.cur, mv.new)
        try:
            self._ensure_dir(parent(mv.new))
            if os.path.lexists(dst) and not osutil.same_file(src, dst):
                raise FileExistsError(dst)
            os.rename(src, dst)
        except OSError:
            # No rename possible: treat it as a deletion plus a new file instead.
            if mv.old in self.m.files:
                self._late_archives.append((mv.old, self.m.files[mv.old][0]))
            self._late_copies.append(CopyOp(mv.new, mv.size, mv.mtime_ns, "new"))
            self.ev.extra_work(1, mv.size)
            return
        entry = self.m.files.pop(mv.old)
        self.m.files[mv.new] = [entry[0], mv.mtime_ns, entry[2]]
        self.res.moved_files += 1
        self.res.log("moved     %s -> %s" % (mv.old, mv.new))

    def _move_dir(self, dm):
        src = osutil.join(self.cur, dm.old)
        dst = osutil.join(self.cur, dm.new)
        try:
            self._ensure_dir(parent(dm.new))
            if os.path.lexists(dst):
                raise FileExistsError(dst)
            os.rename(src, dst)
        except OSError:
            for mv in dm.files:
                self._move_file(mv)
            self._prune(dm.old)
            return
        for mv in dm.files:
            entry = self.m.files.pop(mv.old)
            self.m.files[mv.new] = [entry[0], mv.mtime_ns, entry[2]]
        for rel in [d for d in self.m.dirs if under(d, dm.old)]:
            self.m.dirs.discard(rel)
            self.m.dirs.add(dm.new + rel[len(dm.old):])
        self.res.moved_dirs += 1
        self.res.moved_files += len(dm.files)
        self.res.log("moved     %s/ -> %s/ (%d files, one rename)" % (dm.old, dm.new, len(dm.files)))

    # ------------------------------------------------------------------ archiving deletions

    def _count_archived(self, files, size):
        entry = self.m.archives.setdefault(self.date, {"files": 0, "bytes": 0})
        entry["files"] += files
        entry["bytes"] += size
        self.res.archived_files += files
        self.res.archived_bytes += size

    def _archive_target(self, rel):
        target = osutil.join(self.arch, rel)
        folder = os.path.dirname(target)
        if folder not in self._arch_dirs:
            os.makedirs(folder, exist_ok=True)
            self._arch_dirs.add(folder)
        if os.path.lexists(target):
            stem, ext = os.path.splitext(target)
            number = 2
            while os.path.lexists("%s (%d)%s" % (stem, number, ext)):
                number += 1
            target = "%s (%d)%s" % (stem, number, ext)
        return target

    def _archive_file(self, item):
        rel, size = item
        try:
            os.rename(osutil.join(self.cur, rel), self._archive_target(rel))
        except FileNotFoundError:
            self.m.files.pop(rel, None)   # the index was out of date; nothing to archive
            return
        except OSError as exc:
            self.res.skipped.append((rel, "could not be archived: %s" % reason(exc)))
            return
        self.m.files.pop(rel, None)
        self._count_archived(1, size)
        self.res.log("archived  %s" % rel)

    def _archive_dir(self, ad):
        src = osutil.join(self.cur, ad.rel)
        dst = osutil.join(self.arch, ad.rel)
        try:
            if os.path.lexists(dst):
                raise FileExistsError(dst)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.rename(src, dst)
        except OSError:
            for item in ad.files:
                self._archive_file(item)
            self._prune(ad.rel)
            return
        for rel, _size in ad.files:
            self.m.files.pop(rel, None)
        self._forget_dirs(ad.rel)
        self._count_archived(len(ad.files), sum(size for _rel, size in ad.files))
        self.res.log("archived  %s/ (%d files, one rename)" % (ad.rel, len(ad.files)))

    # ------------------------------------------------------------------ copying

    def _copy(self, op):
        rel = op.rel
        src = osutil.join(self.src, rel)
        dst = osutil.join(self.cur, rel)
        tmp = dst + TMP_SUFFIX
        self.ev.file_start(rel, op.size)
        try:
            fd = osutil.open_read(src)
        except OSError as exc:
            self._skip(op, self._source_problem(exc))
            return
        try:
            with os.fdopen(fd, "rb", buffering=0) as fsrc:
                st = os.fstat(fsrc.fileno())
                if self._already_there(dst, st, self.m.files.get(rel)):
                    # A copy from an interrupted run: nothing to write.
                    self.m.files[rel] = [st.st_size, st.st_mtime_ns, None]
                    self.res.resumed_files += 1
                    self.ev.file_end(rel, "resumed", op.size)
                    return
                copied, digest = self._stream(fsrc, tmp, st.st_size, parent(rel))
            try:
                os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
                osutil.copy_attributes(st, tmp)
                osutil.replace(tmp, dst)
            except OSError as exc:
                raise _WriteError(exc)
        except _ReadError as exc:
            self._discard(tmp)
            self._skip(op, self._source_problem(exc.args[0]))
            return
        except _WriteError as exc:
            self._discard(tmp)
            self._skip(op, self._dest_problem(exc.args[0]))
            return
        except BaseException:
            self._discard(tmp)
            raise
        self.m.files[rel] = [copied, st.st_mtime_ns, digest]
        self.res.copied_files += 1
        self.res.copied_bytes += copied
        self.res.log("%-9s %s" % ("copied" if op.kind == "new" else "updated", rel))
        self.ev.file_end(rel, "copied", copied)

    def _already_there(self, dst, src_stat, known):
        """Is the HDD copy already this exact version? (Resuming after a crash.)"""
        if known and known[0] == src_stat.st_size:
            return False   # same size as the version on record: only a copy is certain
        try:
            dst_stat = os.stat(dst)
        except OSError:
            return False
        return (dst_stat.st_size == src_stat.st_size
                and abs(dst_stat.st_mtime_ns - src_stat.st_mtime_ns) <= HEAL_TOLERANCE_NS)

    def _open_tmp(self, tmp, folder):
        try:
            self._ensure_dir(folder)
            return osutil.open_write(tmp)
        except FileNotFoundError:
            # the index listed a folder the HDD no longer has
            os.makedirs(os.path.dirname(tmp), exist_ok=True)
            return osutil.open_write(tmp)

    def _stream(self, fsrc, tmp, size, folder):
        try:
            out = self._open_tmp(tmp, folder)
        except OSError as exc:
            raise _WriteError(exc)
        digest = hashlib.blake2b(digest_size=16)
        copied = 0
        with os.fdopen(out, "wb", buffering=0) as fdst:
            if size > CHUNK:
                osutil.preallocate(fdst.fileno(), size)
            while True:
                try:
                    count = fsrc.readinto(self._buf)
                except OSError as exc:
                    raise _ReadError(exc)
                if not count:
                    break
                chunk = self._view[:count]
                digest.update(chunk)
                try:
                    written = 0
                    while written < count:
                        written += fdst.write(chunk[written:])
                except OSError as exc:
                    raise _WriteError(exc)
                copied += count
                self.ev.advance(count)
        return copied, digest.hexdigest()

    def _discard(self, tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass

    def _skip(self, op, why):
        self.res.skipped.append((op.rel, why))
        self.ev.file_end(op.rel, "skipped", op.size)

    def _source_problem(self, exc):
        if not isinstance(exc, FileNotFoundError) and not os.path.isdir(self.src):
            raise BackupError("The source drive is no longer reachable.")
        return reason(exc)

    def _dest_problem(self, exc):
        if getattr(exc, "errno", None) == errno.ENOSPC:
            raise BackupError("The backup drive is full.")
        if not os.path.isdir(self.cur):
            raise BackupError("The backup drive is no longer reachable.")
        return "could not be written: %s" % reason(exc)


def history_entry(plan, result, started, seconds):
    return {
        "ts": int(started),
        "seconds": round(seconds, 1),
        "copied_files": result.copied_files,
        "copied_bytes": result.copied_bytes,
        "moved_files": result.moved_files,
        "archived_files": result.archived_files,
        "skipped": len(result.skipped),
        "total_files": plan.scan_files,
        "total_bytes": plan.scan_bytes,
        "stopped": result.stopped,
    }


def log_lines(source, dest, plan, result, started, seconds):
    """The text of a run's log file."""
    lines = [
        "DiskBackup run, %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)),
        "source: %s" % source,
        "backup: %s" % dest,
        "",
        "copied:    %d files, %d bytes" % (result.copied_files, result.copied_bytes),
        "moved:     %d files (%d whole folders)" % (result.moved_files, result.moved_dirs),
        "archived:  %d files, %d bytes" % (result.archived_files, result.archived_bytes),
        "unchanged: %d files, %d bytes" % (plan.unchanged_files, plan.unchanged_bytes),
        "skipped:   %d" % len(result.skipped),
        "duration:  %.1f s" % seconds,
        "stopped early: %s" % ("yes" if result.stopped else "no"),
    ]
    if result.skipped:
        lines += ["", "Skipped:"] + ["  %s: %s" % item for item in result.skipped]
    if plan.unreadable:
        lines += ["", "Could not be read on the source (left as they are in the backup):"]
        lines += ["  %s: %s" % item for item in sorted(plan.unreadable.items())]
    if result.warnings:
        lines += ["", "Warnings:"] + ["  " + text for text in result.warnings]
    lines += ["", "Operations:"] + ["  " + text for text in result.details]
    if result.details_dropped:
        lines.append("  ... and %d more" % result.details_dropped)
    return lines
