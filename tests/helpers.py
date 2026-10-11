"""Shared scaffolding for the tests: a throwaway source and backup folder."""

import os
import shutil
import tempfile
import unittest

from diskbackup import osutil, planner, scanner
from diskbackup.executor import Events, Executor
from diskbackup.manifest import CURRENT, Manifest

DAY = "2026-01-01"
SECOND = 1_000_000_000


class Workspace(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="diskbackup-test-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.src = os.path.join(self.root, "ssd")
        self.dst = os.path.join(self.root, "hdd")
        os.makedirs(self.src)
        os.makedirs(self.dst)
        self._clock = 1_700_000_000 * SECOND

    # ------------------------------------------------------------------ building the source

    def write(self, rel, data=b"data", mtime_ns=None):
        """Create or overwrite a source file, giving it a distinct modified time."""
        path = os.path.join(self.src, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        if mtime_ns is None:
            self._clock += 10 * SECOND
            mtime_ns = self._clock
        os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def touch(self, rel):
        """Give a source file a new modified time without changing its contents."""
        self._clock += 10 * SECOND
        os.utime(self.s(rel), ns=(self._clock, self._clock))

    def s(self, rel):
        return os.path.join(self.src, *rel.split("/"))

    def cur(self, rel=""):
        return os.path.join(self.dst, CURRENT, *rel.split("/")) if rel else os.path.join(self.dst, CURRENT)

    def arch(self, rel, day=DAY):
        return os.path.join(self.dst, day, *rel.split("/"))

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    # ------------------------------------------------------------------ running a backup

    def plan(self, manifest=None, excludes=()):
        manifest = manifest if manifest is not None else Manifest.load(self.dst)
        scan = scanner.scan(self.src, excludes)
        src_root = osutil.fs_path(self.src)
        self.fingerprinted = []

        def fingerprint(rel):
            self.fingerprinted.append(rel)
            try:
                return scanner.fingerprint(osutil.join(src_root, rel))
            except OSError:
                return None

        return manifest, planner.build_plan(scan, manifest, fingerprint)

    def backup(self, day=DAY, excludes=(), events=None, save=True):
        """One full run. Returns (plan, result or None, manifest)."""
        manifest, plan = self.plan(excludes=excludes)
        result = None
        if plan.touches_hdd:
            result = Executor(self.src, self.dst, manifest, plan, events or Events(), day).run()
        elif plan.retimed:
            for rel, mtime_ns in plan.retimed.items():
                manifest.files[rel][1] = mtime_ns
        if save and not plan.is_empty:
            manifest.save(self.dst)
        return plan, result, manifest

    def tree(self, top):
        """{relative path: contents} for every file below `top`."""
        found = {}
        for folder, _dirs, files in os.walk(top):
            for name in files:
                path = os.path.join(folder, name)
                found[os.path.relpath(path, top).replace(os.sep, "/")] = self.read(path)
        return found

    def assertMirrored(self):
        self.assertEqual(self.tree(self.src), self.tree(self.cur()))
