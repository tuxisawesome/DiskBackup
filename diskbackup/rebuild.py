"""Rebuild the index by looking at what is actually on the HDD.

This is the one operation that walks the HDD's folders. It is for adopting a
backup that was made some other way, or for after the HDD was edited by hand.
"""

import os
import re

from . import osutil, scanner
from .executor import HEAL_TOLERANCE_NS, TMP_SUFFIX
from .manifest import CURRENT, Manifest

DATE_FOLDER = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RebuildStats:
    def __init__(self):
        self.matched = 0        # same size and time on both drives
        self.verified = 0       # same size, different time, contents compared equal
        self.different = 0      # on both drives but not the same; next backup recopies
        self.only_hdd = 0       # not on the source any more; next backup archives
        self.only_source = 0    # not yet backed up
        self.leftovers = 0      # unfinished temp files removed


def rebuild(source, dest, excludes, previous=None, on_scan=None, on_work=None, on_bytes=None):
    """Return (Manifest, RebuildStats) describing the HDD as it is now.

    Files that match the source by size and modified time are adopted without
    being read. Files with the same size but a different time are read on both
    drives and compared.
    """
    stats = RebuildStats()
    src_root = osutil.fs_path(source)
    cur_root = os.path.join(osutil.fs_path(dest), CURRENT)

    src = scanner.scan(source, excludes, on_progress=on_scan)
    if os.path.isdir(cur_root):
        hdd = scanner.scan(cur_root, (), on_progress=on_scan)
    else:
        hdd = scanner.ScanResult()

    manifest = Manifest()
    if previous is not None:
        manifest.history = list(previous.history)
        manifest.rate = previous.rate
        manifest.overhead = previous.overhead
    manifest.dirs = set(hdd.dirs)

    doubtful = []
    for rel, (size, mtime_ns) in hdd.files.items():
        if rel.endswith(TMP_SUFFIX):
            try:
                os.remove(osutil.join(cur_root, rel))
                stats.leftovers += 1
            except OSError:
                pass
            continue
        on_source = src.files.get(rel)
        if on_source is None:
            manifest.files[rel] = [size, mtime_ns, None]
            stats.only_hdd += 1
        elif on_source[0] == size and abs(on_source[1] - mtime_ns) <= HEAL_TOLERANCE_NS:
            manifest.files[rel] = [size, on_source[1], None]
            stats.matched += 1
        elif on_source[0] == size and size:
            doubtful.append(rel)
        else:
            manifest.files[rel] = [size, mtime_ns, None]
            stats.different += 1
    stats.only_source = sum(1 for rel in src.files if rel not in hdd.files)

    if on_work:
        on_work(len(doubtful), 2 * sum(hdd.files[rel][0] for rel in doubtful))
    buf = bytearray(scanner.CHUNK)
    for rel in doubtful:
        size, mtime_ns = hdd.files[rel]
        try:
            ours = scanner.fingerprint(osutil.join(src_root, rel), on_bytes, buf)
            theirs = scanner.fingerprint(osutil.join(cur_root, rel), on_bytes, buf)
        except OSError:
            ours, theirs = None, ""
        if ours == theirs:
            manifest.files[rel] = [size, src.files[rel][1], ours]
            stats.verified += 1
        else:
            manifest.files[rel] = [size, mtime_ns, None]
            stats.different += 1

    manifest.archives = measure_archives(dest)
    manifest.dirty = True
    return manifest, stats


def measure_archives(dest):
    """Size up every dated folder of deleted files on the HDD."""
    archives = {}
    root = osutil.fs_path(dest)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return archives
    for name in names:
        path = os.path.join(root, name)
        if not DATE_FOLDER.match(name) or not os.path.isdir(path):
            continue
        try:
            found = scanner.scan(path)
        except OSError:
            continue
        archives[name] = {"files": len(found.files), "bytes": found.bytes}
    return archives
